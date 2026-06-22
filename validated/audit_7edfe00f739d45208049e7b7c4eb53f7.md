### Title
RBF `replace_count` Overcounts Shared Descendants, Causing False Rejection of Legitimate Replacements - (File: `tx-pool/src/pool.rs`)

---

### Summary

In `TxPool::check_rbf`, the `replace_count` guard against Rule #5 (`MAX_REPLACEMENT_CANDIDATES = 100`) accumulates the raw per-conflict descendant-set size for every conflict in a loop, without deduplicating descendants that are shared across multiple conflicts. When two or more conflicting transactions share a common descendant sub-graph, `replace_count` exceeds the true unique replacement count, causing the node to reject a legitimate RBF attempt that would only evict ≤ 100 unique transactions.

---

### Finding Description

`check_rbf` in `tx-pool/src/pool.rs` iterates over every direct conflict and adds `descendants.len() + 1` to `replace_count`:

```rust
// tx-pool/src/pool.rs  lines 613-624
let mut replace_count: usize = 0;
for conflict in conflicts.iter() {
    let descendants = self.pool_map.calc_descendants(&conflict.id);
    replace_count += descendants.len() + 1;          // ← absolute per-conflict count
    if replace_count > MAX_REPLACEMENT_CANDIDATES {
        return Err(Reject::RBFRejected(format!(
            "Tx conflict with too many txs, conflict txs count: {}, expect <= {}",
            replace_count, MAX_REPLACEMENT_CANDIDATES,
        )));
    }
    ...
}
```

`calc_descendants` returns a `HashSet<ProposalShortId>` of all descendants of a single transaction. If two conflicts A and D share a descendant sub-graph (e.g., transaction B spends outputs from both A and D, and B has further children C₁…Cₙ), then every shared descendant is counted once per conflict that owns it, inflating `replace_count` far above the true unique replacement count.

The fee-calculation path (`calculate_min_replace_fee`) correctly deduplicates via a `HashMap`, so only the guard check is affected. [1](#0-0) [2](#0-1) 

---

### Impact Explanation

An attacker who controls transactions in the mempool can construct a diamond-shaped dependency graph:

- **A** spends input I₁ (conflict with victim's replacement)
- **D** spends input I₂ (conflict with victim's replacement)
- **B** spends one output of A **and** one output of D → B is a descendant of both A and D
- **C₁…C₄₉** each spend B's output → all 49 are descendants of both A and D

Pool state:
- `calc_descendants(A)` = {B, C₁…C₄₉} → 50 entries
- `calc_descendants(D)` = {B, C₁…C₄₉} → 50 entries
- `replace_count` = 51 + 51 = **102 > 100** → replacement **rejected**
- Unique transactions to evict = {A, D, B, C₁…C₄₉} = **52 ≤ 100** → should be **allowed**

The victim's RBF replacement is permanently blocked as long as the attacker's pool state persists. The victim cannot unstick their transaction via RBF even though the protocol limit is not actually exceeded. [3](#0-2) 

---

### Likelihood Explanation

Any unprivileged tx-pool submitter can execute this attack. The attacker only needs to:
1. Observe which inputs the victim's original transaction spends (public mempool data).
2. Submit a diamond-shaped dependency graph rooted at two transactions that each conflict with one of the victim's inputs.
3. The attack persists until the attacker's transactions are mined or evicted.

Submitting ~52 low-fee transactions is cheap. No privileged access, no key material, and no majority hashpower is required. The entry path is the standard `send_transaction` RPC / P2P relay. [4](#0-3) 

---

### Recommendation

Replace the per-conflict accumulation with a single deduplicated set across all conflicts, then check its cardinality once:

```rust
// Collect all unique transactions to be replaced
let mut all_to_replace: HashSet<ProposalShortId> = HashSet::new();
for conflict in conflicts.iter() {
    all_to_replace.insert(conflict.id.clone());
    let descendants = self.pool_map.calc_descendants(&conflict.id);
    all_to_replace.extend(descendants);
}
if all_to_replace.len() > MAX_REPLACEMENT_CANDIDATES {
    return Err(Reject::RBFRejected(format!(
        "Tx conflict with too many txs, conflict txs count: {}, expect <= {}",
        all_to_replace.len(), MAX_REPLACEMENT_CANDIDATES,
    )));
}
```

This mirrors the fix in the reference report: charge/count only the **incremental unique additions** rather than the **absolute per-source total**. [5](#0-4) 

---

### Proof of Concept

1. Enable RBF (`min_rbf_rate > min_fee_rate`).
2. Attacker submits:
   - **A**: spends confirmed UTXO I₁, outputs O_A
   - **D**: spends confirmed UTXO I₂, outputs O_D
   - **B**: spends O_A and O_D (child of both A and D), outputs O_B
   - **C₁…C₄₉**: each spends O_B (or a chain from O_B)
3. Victim submits replacement **N** spending I₁ and I₂ with a higher fee.
4. `check_rbf` finds `conflicts = [A, D]`.
   - Loop iteration 1 (A): `descendants = {B, C₁…C₄₉}` (50 items), `replace_count = 51`
   - Loop iteration 2 (D): `descendants = {B, C₁…C₄₉}` (50 items), `replace_count = 102`
   - `102 > MAX_REPLACEMENT_CANDIDATES (100)` → `Reject::RBFRejected("Tx conflict with too many txs…")`
5. Unique transactions that would actually be evicted: {A, D, B, C₁…C₄₉} = 52 — well within the limit.
6. Victim's replacement is permanently blocked. [6](#0-5) [7](#0-6)

### Citations

**File:** tx-pool/src/pool.rs (L31-33)
```rust
const CONFLICTES_CACHE_SIZE: usize = 10_000;
const CONFLICTES_INPUTS_CACHE_SIZE: usize = 30_000;
const MAX_REPLACEMENT_CANDIDATES: usize = 100;
```

**File:** tx-pool/src/pool.rs (L101-115)
```rust
    /// min_replace_fee = sum(replaced_txs.fee) + extra_rbf_fee
    fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
        // don't account for duplicate txs
        let replaced_fees: HashMap<_, _> = conflicts
            .iter()
            .map(|c| (c.id.clone(), c.inner.fee))
            .collect();
        let replaced_sum_fee = replaced_fees
            .values()
            .try_fold(Capacity::zero(), |acc, x| acc.safe_add(*x));
        let res = replaced_sum_fee.map_or(Err(CapacityError::Overflow), |sum| {
            sum.safe_add(extra_rbf_fee)
        });
        if let Ok(res) = res {
```

**File:** tx-pool/src/pool.rs (L574-585)
```rust
    pub(crate) fn check_rbf(
        &self,
        snapshot: &Snapshot,
        entry: &TxEntry,
    ) -> Result<HashSet<ProposalShortId>, Reject> {
        assert!(self.enable_rbf());
        let tx_inputs: Vec<OutPoint> = entry.transaction().input_pts_iter().collect();
        let conflict_ids = self.pool_map.find_conflict_tx(entry.transaction());

        if conflict_ids.is_empty() {
            return Ok(HashSet::new());
        }
```

**File:** tx-pool/src/pool.rs (L611-646)
```rust
        // Rule #5, the replaced tx's descendants can not more than 100
        // and the ancestor of the new tx don't have common set with the replaced tx's descendants
        let mut replace_count: usize = 0;
        let mut all_conflicted = conflicts.clone();
        let ancestors = self.pool_map.calc_ancestors(&short_id);
        for conflict in conflicts.iter() {
            let descendants = self.pool_map.calc_descendants(&conflict.id);
            replace_count += descendants.len() + 1;
            if replace_count > MAX_REPLACEMENT_CANDIDATES {
                return Err(Reject::RBFRejected(format!(
                    "Tx conflict with too many txs, conflict txs count: {}, expect <= {}",
                    replace_count, MAX_REPLACEMENT_CANDIDATES,
                )));
            }

            if !descendants.is_disjoint(&ancestors) {
                return Err(Reject::RBFRejected(
                    "Tx ancestors have common with conflict Tx descendants".to_string(),
                ));
            }

            let entries = descendants
                .iter()
                .filter_map(|id| self.get_pool_entry(id))
                .collect::<Vec<_>>();

            for entry in entries.iter() {
                let hash = entry.inner.transaction().hash();
                if tx_inputs.iter().any(|pt| pt.tx_hash() == hash) {
                    return Err(Reject::RBFRejected(
                        "new Tx contains inputs in descendants of to be replaced Tx".to_string(),
                    ));
                }
            }
            all_conflicted.extend(entries);
        }
```
