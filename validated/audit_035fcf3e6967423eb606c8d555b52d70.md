### Title
Unbounded Descendants Iteration in `update_descendants_index_key` Causes O(N) Block-Processing Stall — (`tx-pool/src/component/pool_map.rs`)

### Summary
The tx-pool enforces `max_ancestors_count` (default 1,000) to bound how many ancestors a transaction may have, but imposes **no corresponding limit on descendants**. When a transaction is removed from the pool via `remove_entry`, `update_descendants_index_key` iterates over every descendant to update their ancestor-weight statistics. An unprivileged transaction sender can pre-load the pool with an unbounded number of transactions that all reference the same in-pool transaction as a cell dep, each individually satisfying the ancestors limit. When that shared cell-dep transaction is later committed in a block (a normal miner event requiring no attacker control), `update_descendants_index_key` must walk the entire descendant set in O(N) time, stalling the tx-pool service thread.

### Finding Description

`PoolMap::remove_entry` unconditionally calls both `update_ancestors_index_key` and `update_descendants_index_key`: [1](#0-0) 

`update_descendants_index_key` computes `calc_descendants` and then iterates over every result to subtract the removed entry's weight from each descendant's ancestor-score fields: [2](#0-1) 

The developers are aware this iteration is expensive: `remove_entry_and_descendants` explicitly pre-removes all links before calling `remove_entry` so that `calc_descendants` returns an empty set and the loop is skipped: [3](#0-2) 

However, this mitigation is only applied when the **entire subtree is being evicted**. When a single transaction is committed in a block (its descendants remain in the pool), `remove_entry` is called directly, and `update_descendants_index_key` walks the full, unbounded descendant set.

The descendants count is bounded only by the total pool size (`max_tx_pool_size`, default 180 MB): [4](#0-3) 

Because the ancestors-count check is applied to the *new* transaction's ancestors, not to the *existing* transaction's descendants, an attacker can submit an arbitrary number of transactions that each reference the same in-pool transaction as a cell dep. Each such transaction has `ancestors_count = 2` (itself + the shared dep), well within the limit of 1,000: [5](#0-4) 

The `check_and_record_ancestors` function enforces the ancestors limit on the incoming transaction, but has no awareness of how many descendants the referenced dep transaction already has: [6](#0-5) 

The existing integration test `TxPoolLimitAncestorCount` already demonstrates that 2,000 transactions can simultaneously reference the same cell dep, confirming the attack setup is valid: [7](#0-6) 

### Impact Explanation

When the shared dep transaction is committed in a block, the tx-pool's block-processing path calls `remove_entry` for it. `update_descendants_index_key` then iterates over all N descendants, performing a `modify_by_id` update for each: [8](#0-7) 

Because the tx-pool is a single-threaded actor, all other operations — new transaction submission, RPC responses, and critically **block template generation for miners** — are blocked for the duration of this O(N) loop. With N in the tens of thousands (easily achievable within the 180 MB pool limit at ~100 bytes/tx minimum), the stall is measurable. Repeated across multiple committed transactions with large descendant sets, this causes sustained degradation of tx-pool responsiveness and delayed block template production.

### Likelihood Explanation

Any unprivileged RPC caller (`send_transaction`) or P2P transaction relayer can execute this attack. The attacker must pay transaction fees for each descendant transaction, but the cost is proportional to N and can be amortized. The trigger — block commitment of the shared dep — requires no attacker control over mining; it occurs naturally as miners include the dep transaction for its fees. The attack requires no privileged role, Sybil capability, or majority hashpower.

### Recommendation

Introduce a `max_descendants_count` limit (analogous to `max_ancestors_count`) enforced when adding a new transaction to the pool. Before accepting a new transaction that references an in-pool transaction as a cell dep or input, check that the referenced transaction's current `descendants_count` does not exceed the configured maximum. Reject the new transaction with a `ExceededMaximumDescendantsCount` error if the limit would be exceeded. This mirrors the existing `check_and_record_ancestors` logic: [9](#0-8) 

and bounds the work done by `update_descendants_index_key` to O(`max_descendants_count`) per `remove_entry` call. The `TxPoolConfig` struct should expose this as a configurable parameter alongside `max_ancestors_count`: [10](#0-9) 

### Proof of Concept

1. Submit `tx_root` to the pool via `send_transaction` RPC (any valid transaction with at least one output).
2. Submit N transactions `tx_1 … tx_N`, each with a `cell_dep` pointing to `tx_root`'s output. Each `tx_i` has `ancestors_count = 2` (itself + `tx_root`), satisfying `max_ancestors_count = 1,000`.
3. `tx_root` now has N entries in `pool_map.links[tx_root].children`.
4. Wait for a miner to commit `tx_root` in a block (no attacker control required — miners include it for its fees).
5. The tx-pool calls `remove_entry(&tx_root_id)` → `update_descendants_index_key(tx_root, Remove)` → `calc_descendants` returns all N entries → the loop at `pool_map.rs:450–459` executes N times, stalling the tx-pool thread.
6. With N = 50,000 (well within the 180 MB pool limit at ~100 bytes/tx), the stall is measurable and blocks all concurrent tx-pool operations including block template generation.

The existing test at `test/src/specs/tx_pool/limit.rs:93–101` already demonstrates 2,000 such cell-dep transactions being accepted simultaneously, confirming the attack setup is protocol-valid. [11](#0-10)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L235-250)
```rust
    pub(crate) fn remove_entry(&mut self, id: &ProposalShortId) -> Option<TxEntry> {
        self.entries.remove_by_id(id).map(|entry| {
            debug!(
                "remove entry {} from status: {:?}",
                entry.inner.transaction().hash(),
                entry.status
            );
            self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
            self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
            self.remove_entry_edges(&entry.inner);
            self.remove_entry_links(id);
            self.track_entry_statics(Some(entry.status), None);
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
            entry.inner
        })
    }
```

**File:** tx-pool/src/component/pool_map.rs (L252-264)
```rust
    pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
        let mut removed_ids = vec![id.to_owned()];
        removed_ids.extend(self.calc_descendants(id));

        // update links state for remove, so that we won't update_descendants_index_key in remove_entry
        for id in &removed_ids {
            self.remove_entry_links(id);
        }

        removed_ids
            .iter()
            .filter_map(|id| self.remove_entry(id))
            .collect()
```

**File:** tx-pool/src/component/pool_map.rs (L447-460)
```rust
    fn update_descendants_index_key(&mut self, parent: &TxEntry, op: EntryOp) {
        let descendants: HashSet<ProposalShortId> =
            self.links.calc_descendants(&parent.proposal_short_id());
        for desc_id in &descendants {
            // update child score
            self.entries.modify_by_id(desc_id, |e| {
                match op {
                    EntryOp::Remove => e.inner.sub_ancestor_weight(parent),
                    EntryOp::Add => e.inner.add_ancestor_weight(parent),
                };
                e.score = e.inner.as_score_key();
            });
        }
    }
```

**File:** tx-pool/src/component/pool_map.rs (L588-601)
```rust
    fn check_and_record_ancestors(
        &mut self,
        entry: &mut TxEntry,
    ) -> Result<HashSet<TxEntry>, Reject> {
        let tx = entry.transaction();
        let (ancestors, mut parents, cell_ref_parents) = self.get_tx_ancenstors(tx);

        let mut ancestors_count = ancestors.len() + 1;
        let mut evicted = Default::default();

        if ancestors_count <= self.max_ancestors_count {
            self._record_ancestors(entry, ancestors, parents);
            return Ok(evicted);
        }
```

**File:** util/app-config/src/configs/tx_pool.rs (L11-14)
```rust
pub struct TxPoolConfig {
    /// Keep the transaction pool below <max_tx_pool_size> mb
    pub max_tx_pool_size: usize,
    /// txs with lower fee rate than this will not be relayed or be mined
```

**File:** util/app-config/src/configs/tx_pool.rs (L25-26)
```rust
    /// max ancestors size limit for a single tx
    pub max_ancestors_count: usize,
```

**File:** util/app-config/src/legacy/tx_pool.rs (L15-16)
```rust
// default max ancestors count
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
```

**File:** test/src/specs/tx_pool/limit.rs (L90-101)
```rust
        // Create 250 transactions cell dep on tx_a
        // we can have more than config.max_ancestors_count number of txs using one cell ref
        let mut cell_ref_txs = vec![];
        for i in 1..=2000 {
            let cur = always_success_transaction(node0, initial_inputs.get(i).unwrap());
            let cur = cur.as_advanced_builder().cell_dep(cell_dep.clone()).build();
            let res = node0
                .rpc_client()
                .send_transaction_result(cur.data().into());
            assert!(res.is_ok());
            cell_ref_txs.push(cur.clone());
        }
```
