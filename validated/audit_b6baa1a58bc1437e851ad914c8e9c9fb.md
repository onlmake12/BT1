### Title
RBF Replacement Permanently Blocked via Descendant Transaction Flooding — (`tx-pool/src/pool.rs`)

---

### Summary

CKB's Replace-By-Fee (RBF) logic enforces a hard cap of `MAX_REPLACEMENT_CANDIDATES = 100` on the total number of transactions that would be evicted by a replacement. Because there is **no corresponding limit on how many descendants a pooled transaction may accumulate**, any party who can spend a transaction's outputs can pre-populate the pool with ≥ 100 cheap descendant transactions and permanently block RBF replacement of the pinned transaction. This is structurally identical to the Ajna H-6 finding: an attacker pre-populates a bounded data structure with dust entries to exhaust an iteration/count limit and stall a mandatory protocol action.

---

### Finding Description

`check_rbf` in `tx-pool/src/pool.rs` implements Rule #5 of CKB's RBF policy:

```rust
// Rule #5, the replaced tx's descendants can not more than 100
let mut replace_count: usize = 0;
for conflict in conflicts.iter() {
    let descendants = self.pool_map.calc_descendants(&conflict.id);
    replace_count += descendants.len() + 1;
    if replace_count > MAX_REPLACEMENT_CANDIDATES {          // hard cap = 100
        return Err(Reject::RBFRejected(format!(
            "Tx conflict with too many txs, conflict txs count: {}, expect <= {}",
            replace_count, MAX_REPLACEMENT_CANDIDATES,
        )));
    }
``` [1](#0-0) [2](#0-1) 

`calc_descendants` performs an unbounded BFS/DFS over the `TxLinksMap` children graph:

```rust
pub fn calc_descendants(&self, short_id: &ProposalShortId) -> HashSet<ProposalShortId> {
    self.calc_relative_ids(short_id, Relation::Children)
}
``` [3](#0-2) [4](#0-3) 

The pool enforces `max_ancestors_count` (default 25) to cap how many ancestors a single transaction may have, but there is **no `max_descendants_count`** enforced at insertion time: [5](#0-4) [6](#0-5) 

Because the ancestor limit is per-transaction (not per-tree), an attacker can build a **wide tree** of descendants. For example, if the pinned transaction T has two outputs, the attacker creates two children (each with 1 ancestor), four grandchildren (each with 2 ancestors), and so on. At depth 7 the tree already has 2⁷ = 128 descendants, every one of which satisfies the 25-ancestor limit. The total cost at the minimum fee rate of 1 000 shannons/KB for ~200-byte transactions is roughly 100 × 200 shannons = **20 000 shannons ≈ 0.0002 CKB** — negligible.

Once the descendant count exceeds 100, every subsequent RBF attempt against T is rejected unconditionally before any fee comparison is performed. The attacker can refresh expiring descendants (default TTL 12 h) to maintain the pin indefinitely. [7](#0-6) 

---

### Impact Explanation

**Transaction pinning**: any party who can spend a pooled transaction's outputs (e.g., a payment recipient, a multisig co-signer, or the original sender themselves) can prevent that transaction from ever being replaced via RBF. Concretely:

- A recipient Bob can pin a payment from Alice, preventing Alice from fee-bumping or cancelling it.
- A low-fee transaction can be kept alive in the pool indefinitely, blocking the slot for a higher-fee double-spend.
- Combined with the two-phase proposal/commit window, a pinned transaction that is proposed but not yet committed can stall the commitment of a competing transaction for the entire proposal window.

---

### Likelihood Explanation

- The attack requires no special privilege: any RPC caller or peer who can submit transactions can execute it.
- The economic cost is negligible (~0.0002 CKB for 100 descendants).
- The attack is self-sustaining: the attacker simply re-submits expired descendants before the 12-hour TTL elapses.
- No on-chain action is required; the entire attack lives in the mempool.

---

### Recommendation

1. **Enforce a `max_descendants_count` at insertion time** (symmetric with `max_ancestors_count`). Reject any transaction whose addition would cause an existing pool entry to exceed the limit. This mirrors the Ajna recommendation of controlling `addQuoteToken()` for dust.
2. **Alternatively**, raise `MAX_REPLACEMENT_CANDIDATES` and simultaneously cap the descendant tree size at insertion, so the two limits are consistent.
3. **At minimum**, document the pinning risk and advise operators to set `min_fee_rate` high enough that maintaining 100 descendants is economically meaningful.

---

### Proof of Concept

```
1. Submit transaction T with ≥ 2 outputs (e.g., via send_transaction RPC).
2. Submit T_1  spending T.output[0]  (1 ancestor).
   Submit T_2  spending T.output[1]  (1 ancestor).
   Submit T_11 spending T_1.output[0] (2 ancestors).
   Submit T_12 spending T_1.output[1] (2 ancestors).
   ... continue until total descendants of T ≥ 100
   (depth 7 with branching factor 2 yields 128 descendants,
    each with ≤ 7 ancestors, well within max_ancestors_count=25).

3. Attempt to RBF-replace T with T' (higher fee, same inputs).
   → check_rbf counts replace_count = 128 + 1 > 100
   → returns Err(RBFRejected("Tx conflict with too many txs …"))

4. T remains pinned. Repeat step 2 before descendants expire (12 h TTL).
``` [8](#0-7) [9](#0-8)

### Citations

**File:** tx-pool/src/pool.rs (L33-33)
```rust
const MAX_REPLACEMENT_CANDIDATES: usize = 100;
```

**File:** tx-pool/src/pool.rs (L574-679)
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

        let short_id = entry.proposal_short_id();

        // Rule #1, the node has enabled RBF, which is checked by caller
        let conflicts = conflict_ids
            .iter()
            .filter_map(|id| self.get_pool_entry(id))
            .collect::<Vec<_>>();
        assert!(conflicts.len() == conflict_ids.len());

        // Rule #2, new tx don't contain any new unconfirmed inputs
        let mut inputs = HashSet::new();
        for c in conflicts.iter() {
            inputs.extend(c.inner.transaction().input_pts_iter());
        }

        if tx_inputs
            .iter()
            .any(|pt| !inputs.contains(pt) && !snapshot.transaction_exists(&pt.tx_hash()))
        {
            return Err(Reject::RBFRejected(
                "new Tx contains unconfirmed inputs".to_string(),
            ));
        }

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

        let tx_cells_deps: Vec<OutPoint> = entry
            .transaction()
            .cell_deps_iter()
            .map(|c| c.out_point())
            .collect();
        for entry in all_conflicted.iter() {
            let hash = entry.inner.transaction().hash();
            if tx_cells_deps.iter().any(|pt| pt.tx_hash() == hash) {
                return Err(Reject::RBFRejected(
                    "new Tx contains cell deps from conflicts".to_string(),
                ));
            }
        }

        // Rule #4, new tx's fee need to higher than min_rbf_fee computed from the tx_pool configuration
        // Rule #3, new tx's fee need to higher than conflicts, here we only check the all conflicted txs fee
        let fee = entry.fee;
        if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
            if fee < min_replace_fee {
                return Err(Reject::RBFRejected(format!(
                    "Tx's current fee is {}, expect it to >= {} to replace old txs",
                    fee, min_replace_fee,
                )));
            }
        } else {
            return Err(Reject::RBFRejected(
                "calculate_min_replace_fee failed".to_string(),
            ));
        }

        Ok(conflict_ids)
    }
```

**File:** tx-pool/src/component/links.rs (L37-72)
```rust
    fn calc_relative_ids(
        &self,
        short_id: &ProposalShortId,
        relation: Relation,
    ) -> HashSet<ProposalShortId> {
        let direct = self
            .inner
            .get(short_id)
            .map(|link| link.get_direct_ids(relation))
            .cloned()
            .unwrap_or_default();

        self.calc_relation_ids(direct, relation)
    }

    pub fn calc_relation_ids(
        &self,
        mut stage: HashSet<ProposalShortId>,
        relation: Relation,
    ) -> HashSet<ProposalShortId> {
        let mut relation_ids = HashSet::with_capacity(stage.len());

        while let Some(id) = stage.iter().next().cloned() {
            //recursively
            if let Some(tx_links) = self.inner.get(&id) {
                for direct_id in tx_links.get_direct_ids(relation) {
                    if !relation_ids.contains(direct_id) {
                        stage.insert(direct_id.clone());
                    }
                }
            }
            stage.remove(&id);
            relation_ids.insert(id);
        }
        relation_ids
    }
```

**File:** tx-pool/src/component/links.rs (L82-84)
```rust
    pub fn calc_descendants(&self, short_id: &ProposalShortId) -> HashSet<ProposalShortId> {
        self.calc_relative_ids(short_id, Relation::Children)
    }
```

**File:** tx-pool/src/component/pool_map.rs (L178-181)
```rust
    /// calculate all descendants from pool
    pub(crate) fn calc_descendants(&self, short_id: &ProposalShortId) -> HashSet<ProposalShortId> {
        self.links.calc_descendants(short_id)
    }
```

**File:** tx-pool/src/component/pool_map.rs (L595-601)
```rust
        let mut ancestors_count = ancestors.len() + 1;
        let mut evicted = Default::default();

        if ancestors_count <= self.max_ancestors_count {
            self._record_ancestors(entry, ancestors, parents);
            return Ok(evicted);
        }
```

**File:** util/app-config/src/configs/tx_pool.rs (L25-27)
```rust
    /// max ancestors size limit for a single tx
    pub max_ancestors_count: usize,
    /// rejected tx time to live by days
```

**File:** util/app-config/src/legacy/tx_pool.rs (L17-18)
```rust
// Default expiration time for pool transactions in hours
const DEFAULT_EXPIRY_HOURS: u8 = 12;
```
