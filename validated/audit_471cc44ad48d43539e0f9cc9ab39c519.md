### Title
Overcounted `replace_count` in `check_rbf` Due to Non-Deduplicated Shared Descendants Causes RBF DoS — (`tx-pool/src/pool.rs`)

### Summary

In `TxPool::check_rbf`, the replacement-candidate count (`replace_count`) is accumulated by summing `descendants.len() + 1` for each conflicting transaction independently. When two or more conflicting transactions share common descendants (possible because a CKB transaction can spend outputs from multiple parents), those shared descendants are counted multiple times. This causes `replace_count` to exceed `MAX_REPLACEMENT_CANDIDATES` (100) even when the actual number of unique transactions to replace is well within the limit, resulting in a false rejection of a legitimate RBF transaction.

### Finding Description

In `check_rbf` in `tx-pool/src/pool.rs`:

```rust
let mut replace_count: usize = 0;
let mut all_conflicted = conflicts.clone();
for conflict in conflicts.iter() {
    let descendants = self.pool_map.calc_descendants(&conflict.id);
    replace_count += descendants.len() + 1;          // ← no dedup across conflicts
    if replace_count > MAX_REPLACEMENT_CANDIDATES {
        return Err(Reject::RBFRejected(format!(
            "Tx conflict with too many txs, conflict txs count: {}, expect <= {}",
            replace_count, MAX_REPLACEMENT_CANDIDATES,
        )));
    }
    ...
    all_conflicted.extend(entries);                   // ← all_conflicted also gets duplicates
}
``` [1](#0-0) 

`calc_descendants` returns a `HashSet<ProposalShortId>` of all transitive children of a given transaction. [2](#0-1) 

Because CKB transactions can have multiple inputs, a single descendant transaction can legitimately spend outputs from two different conflicting transactions simultaneously. In that case, `calc_descendants` for each conflict will both include that shared descendant, and `replace_count` will count it twice.

**Concrete scenario:**

1. `TX_A` (in pool) spends on-chain cell `X`.
2. `TX_B` (in pool) spends on-chain cell `Y`.
3. `TX_C` (in pool) spends output-0 of `TX_A` **and** output-0 of `TX_B` (two inputs, both from pool parents).
4. Attacker submits `TX_NEW` that spends both `X` and `Y` (RBF attempt).

`find_conflict_tx` returns `{TX_A, TX_B}`. [3](#0-2) 

Loop iteration for `TX_A`: `calc_descendants(TX_A)` = `{TX_C}` → `replace_count += 2`.
Loop iteration for `TX_B`: `calc_descendants(TX_B)` = `{TX_C}` → `replace_count += 2`.
Final `replace_count = 4`, but unique transactions to replace = 3 (`TX_A`, `TX_B`, `TX_C`).

Scaling this up: with 2 conflicts each having 50 shared descendants, the actual unique replacement set is 52 (within the 100 limit), but `replace_count` = 102, triggering a false rejection.

Note that `calculate_min_replace_fee` correctly deduplicates via a `HashMap` keyed by `id`, so the fee check is not affected — only the count check is broken. [4](#0-3) 

### Impact Explanation

Any RBF transaction whose conflict set has shared descendants will be incorrectly rejected with `"Tx conflict with too many txs"` even when the true unique replacement count is within `MAX_REPLACEMENT_CANDIDATES`. This is a targeted DoS on the RBF mechanism: an attacker who controls pool state (by pre-submitting transactions) can permanently block a victim's RBF attempts for specific UTXOs.

### Likelihood Explanation

The attacker-controlled entry path is the `send_transaction` RPC (or P2P relay), accessible to any unprivileged tx-pool submitter. The attacker needs to:
1. Submit `TX_A` spending cell `X`.
2. Submit `TX_B` spending cell `Y`.
3. Submit `TX_C` with two inputs: output of `TX_A` and output of `TX_B`.

This is a valid CKB transaction structure (multiple inputs are standard). No special privilege, key, or majority hashpower is required. The attacker only needs to know which UTXOs the victim intends to RBF-replace.

### Recommendation

Collect all descendants across all conflicts into a single `HashSet` before counting, so shared descendants are deduplicated:

```rust
let mut all_descendants: HashSet<ProposalShortId> = HashSet::new();
for conflict in conflicts.iter() {
    let descendants = self.pool_map.calc_descendants(&conflict.id);
    // disjoint check and input-in-descendants check still per-conflict
    ...
    all_descendants.extend(descendants);
}
// replace_count = unique conflicts + unique descendants
let replace_count = conflicts.len() + all_descendants.len();
if replace_count > MAX_REPLACEMENT_CANDIDATES { ... }
```

This mirrors the deduplication already applied in `calculate_min_replace_fee` via `HashMap`.

### Proof of Concept

Pool state before RBF attempt:
- `TX_A`: input = on-chain cell `X`, output[0] = cell `P`
- `TX_B`: input = on-chain cell `Y`, output[0] = cell `Q`
- `TX_C`: inputs = [output[0] of `TX_A`, output[0] of `TX_B`]

Repeat `TX_C`-style fan-in descendants until 50 shared descendants exist between `TX_A` and `TX_B`.

Submit `TX_NEW` via `send_transaction` RPC spending both `X` and `Y` with sufficient fee.

Expected (correct) behavior: `replace_count` = 52 ≤ 100, RBF proceeds.
Actual behavior: `replace_count` = 2 × 51 = 102 > 100, rejected with `"Tx conflict with too many txs, conflict txs count: 102, expect <= 100"`. [5](#0-4)

### Citations

**File:** tx-pool/src/pool.rs (L104-111)
```rust
        // don't account for duplicate txs
        let replaced_fees: HashMap<_, _> = conflicts
            .iter()
            .map(|c| (c.id.clone(), c.inner.fee))
            .collect();
        let replaced_sum_fee = replaced_fees
            .values()
            .try_fold(Capacity::zero(), |acc, x| acc.safe_add(*x));
```

**File:** tx-pool/src/pool.rs (L611-624)
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
```

**File:** tx-pool/src/component/links.rs (L82-84)
```rust
    pub fn calc_descendants(&self, short_id: &ProposalShortId) -> HashSet<ProposalShortId> {
        self.calc_relative_ids(short_id, Relation::Children)
    }
```

**File:** tx-pool/src/component/pool_map.rs (L294-298)
```rust
    pub(crate) fn find_conflict_tx(&self, tx: &TransactionView) -> HashSet<ProposalShortId> {
        tx.input_pts_iter()
            .filter_map(|out_point| self.edges.get_input_ref(&out_point).cloned())
            .collect()
    }
```
