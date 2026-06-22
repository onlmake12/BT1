### Title
Silent Cross-Pool `ProposalShortId` Collision Overwrites Transaction in Compact Block Reconstruction — (`tx-pool/src/service.rs`)

---

### Summary

`get_tx_for_compact_block` in `tx-pool/src/service.rs` searches three separate internal pools (`verify_queue`, `orphan`, `pool_map`/store) for transactions by `ProposalShortId` and merges results with sequential `extend()` calls. Because `ProposalShortId` is a 10-byte truncation of the transaction hash — explicitly documented throughout the codebase as expected to occasionally collide — a different transaction with the same `ProposalShortId` in a later-searched pool silently overwrites the correct one. The wrong transaction is then fed into `reconstruct_block`, causing a spurious `Collided` result and forcing an unnecessary extra round-trip to the relaying peer for every compact block that triggers the condition.

---

### Finding Description

`get_tx_for_compact_block` is the sole implementation behind `fetch_txs`, which is called by `reconstruct_block` during compact block relay:

```
// tx-pool/src/service.rs  L1178-1208
pub async fn get_tx_for_compact_block(
    &self,
    short_ids: HashSet<ProposalShortId>,
) -> HashMap<ProposalShortId, TransactionView> {
    let mut txs = HashMap::with_capacity(short_ids.len());
    {
        let verify_queue = self.verify_queue.read().await;
        txs.extend(short_ids.iter().filter_map(|short_id| {
            verify_queue.get_tx_by_id(short_id)
                .map(|entry| (short_id.to_owned(), entry.tx.to_owned()))
        }));
    }
    {
        let orphan = self.orphan.read().await;
        txs.extend(short_ids.iter().filter_map(|short_id| {
            orphan.get(short_id)
                .map(|entry| (short_id.to_owned(), entry.tx.to_owned()))
        }));
    }
    {
        let tx_pool = self.tx_pool.read().await;
        txs.extend(short_ids.iter().filter_map(|short_id| {
            tx_pool.get_tx_from_pool_or_store(short_id)
                .map(|tx| (short_id.to_owned(), tx))
        }));
    }
    txs
}
```

Each `extend()` call unconditionally overwrites any prior entry for the same key. There is no check for whether a `ProposalShortId` was already resolved from an earlier pool, and no warning or error is emitted when an overwrite occurs.

The three pools are independent: `check_txid_collision` only guards the main `pool_map`; `add_orphan_tx` only guards the orphan pool. Nothing prevents `tx_A` (short_id `S`) from residing in the orphan pool while `tx_B` (different full hash, same short_id `S`) resides in the verify_queue or pool_map simultaneously.

`ProposalShortId` is the first 10 bytes of the transaction hash. The codebase itself acknowledges this is a collision-prone identifier:

> "Keeping in mind that short_ids are expected to occasionally collide." — `sync/src/relayer/compact_block_process.rs` L26, `sync/src/relayer/block_transactions_process.rs` L12

> "we assume that all the short_ids and prefilled transactions should NOT collide with each other, because in the tx-pool, the node should use short_id as the key." — `sync/src/relayer/compact_block_verifier.rs` L5-7

The second comment reveals the assumption: the tx-pool uses `ProposalShortId` as a key, so within a single pool there is no collision. But `get_tx_for_compact_block` merges across three pools without enforcing this invariant.

When the wrong transaction is placed into `txs_map` and used in `reconstruct_block`, the assembled block's `transactions_root` will not match the compact block header's `transactions_root`. The code then returns `ReconstructionResult::Collided`, causing the node to request all short-ID transactions from the peer again:

```
// sync/src/relayer/mod.rs  L507-526
if compact_block_tx_root != reconstruct_block_tx_root {
    if compact_block.short_ids().is_empty()
        || compact_block.short_ids().len() == block_txs_len
    {
        return ReconstructionResult::Error(...);
    } else {
        return ReconstructionResult::Collided;
    }
}
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

---

### Impact Explanation

When a cross-pool `ProposalShortId` collision exists at the moment a compact block arrives:

1. `get_tx_for_compact_block` silently returns the wrong transaction for that short ID (the one from the last-searched pool wins).
2. `reconstruct_block` assembles a block with the wrong transaction, producing a mismatched `transactions_root`.
3. The node returns `ReconstructionResult::Collided` and sends a `GetBlockTransactions` request back to the peer for all short-ID positions.
4. This adds a full network round-trip to block propagation for the affected node.
5. Repeated triggering of this condition against a node delays its view of the chain tip, degrading its ability to mine on the correct tip and increasing orphan rate.

The `transactions_root` check prevents the wrong transaction from being committed to the chain, so there is no consensus failure. The impact is **relay-layer DoS / block propagation delay**. [6](#0-5) [7](#0-6) 

---

### Likelihood Explanation

`ProposalShortId` is 10 bytes (80 bits). A birthday-paradox collision between any two transactions in a pool of `N` entries becomes non-negligible at `N ≈ 2^40`. For a targeted attack (finding a transaction whose short ID collides with a specific known transaction), the attacker must perform ~2^80 hash evaluations — computationally infeasible today.

However, the condition can arise **naturally** without any attacker:

- A transaction `tx_A` enters the orphan pool (missing parent).
- A different transaction `tx_B` with the same `ProposalShortId` (natural birthday collision in a large mempool) is accepted into the main pool.
- A compact block arrives referencing `tx_A`'s short ID.
- `get_tx_for_compact_block` returns `tx_B`, causing a spurious `Collided` result.

With a mempool of tens of thousands of transactions, the probability of at least one such cross-pool collision is low but non-zero and grows with mempool size. A well-resourced adversary who can submit many transactions to the network can increase this probability deliberately. [8](#0-7) [9](#0-8) 

---

### Recommendation

1. **Detect cross-pool collisions explicitly**: In `get_tx_for_compact_block`, before inserting a result from a later pool, check whether the `short_id` was already resolved from an earlier pool. If the resolved transactions differ (different full hashes), log a warning and do not silently overwrite — instead, treat the slot as ambiguous (omit it, forcing a peer request for the correct transaction).

2. **Prefer the most-trusted pool**: If overwriting is intentional (pool_map is more authoritative than orphan), document this explicitly and verify that the overwriting transaction actually matches the compact block's expected short ID by comparing full hashes against any available hint.

3. **Align with the existing collision-detection path**: The `reconstruct_block` function already handles `Collided` correctly. The fix should ensure that a cross-pool collision is surfaced as a `Collided` result immediately (before assembling the block) rather than discovered only after a failed `transactions_root` check. [10](#0-9) [11](#0-10) 

---

### Proof of Concept

1. Submit transaction `tx_A` to a CKB node such that it enters the **orphan pool** (its parent is missing). `tx_A` has `ProposalShortId` = `S`.

2. Submit transaction `tx_B` (different inputs/outputs, different full hash, but same `ProposalShortId` `S` — achievable via birthday search or natural collision) to the same node. `tx_B` is accepted into the main **pool_map**.

3. A miner broadcasts a compact block containing `tx_A` (referenced by short ID `S`).

4. The receiving node calls `get_tx_for_compact_block({S})`:
   - Step 1 (verify_queue): `S` not found → `txs = {}`
   - Step 2 (orphan): `S` → `tx_A` → `txs = {S: tx_A}`
   - Step 3 (pool_map): `S` → `tx_B` → `txs.extend(...)` → `txs = {S: tx_B}` (**overwrites**)

5. `reconstruct_block` assembles the block with `tx_B` instead of `tx_A`. The `transactions_root` does not match. Since not all transactions came from the peer, `ReconstructionResult::Collided` is returned.

6. The node sends `GetBlockTransactions` back to the peer, adding a full round-trip to block propagation. [12](#0-11) [13](#0-12) [14](#0-13)

### Citations

**File:** tx-pool/src/service.rs (L1178-1208)
```rust
    pub async fn get_tx_for_compact_block(
        &self,
        short_ids: HashSet<ProposalShortId>,
    ) -> HashMap<ProposalShortId, TransactionView> {
        let mut txs = HashMap::with_capacity(short_ids.len());
        {
            let verify_queue = self.verify_queue.read().await;
            txs.extend(short_ids.iter().filter_map(|short_id| {
                verify_queue
                    .get_tx_by_id(short_id)
                    .map(|entry| (short_id.to_owned(), entry.tx.to_owned()))
            }));
        }
        {
            let orphan = self.orphan.read().await;
            txs.extend(short_ids.iter().filter_map(|short_id| {
                orphan
                    .get(short_id)
                    .map(|entry| (short_id.to_owned(), entry.tx.to_owned()))
            }));
        }
        {
            let tx_pool = self.tx_pool.read().await;
            txs.extend(short_ids.iter().filter_map(|short_id| {
                tx_pool
                    .get_tx_from_pool_or_store(short_id)
                    .map(|tx| (short_id.to_owned(), tx))
            }));
        }
        txs
    }
```

**File:** sync/src/relayer/mod.rs (L371-393)
```rust
        let mut short_ids_set: HashSet<ProposalShortId> =
            compact_block.short_ids().into_iter().collect();

        let mut txs_map: HashMap<ProposalShortId, core::TransactionView> = received_transactions
            .into_iter()
            .filter_map(|tx| {
                let short_id = tx.proposal_short_id();
                if short_ids_set.remove(&short_id) {
                    Some((short_id, tx))
                } else {
                    None
                }
            })
            .collect();

        if !short_ids_set.is_empty() {
            let tx_pool = self.shared.shared().tx_pool_controller();
            let fetch_txs = tx_pool.fetch_txs(short_ids_set).await;
            if let Err(e) = fetch_txs {
                return ReconstructionResult::Error(StatusCode::TxPool.with_context(e));
            }
            txs_map.extend(fetch_txs.unwrap());
        }
```

**File:** sync/src/relayer/mod.rs (L507-526)
```rust
            let compact_block_tx_root = compact_block.header().raw().transactions_root();
            let reconstruct_block_tx_root = block.transactions_root();
            if compact_block_tx_root != reconstruct_block_tx_root {
                if compact_block.short_ids().is_empty()
                    || compact_block.short_ids().len() == block_txs_len
                {
                    return ReconstructionResult::Error(
                        StatusCode::CompactBlockHasUnmatchedTransactionRootWithReconstructedBlock
                            .with_context(format!(
                                "Compact_block_tx_root({}) != reconstruct_block_tx_root({})",
                                compact_block.header().raw().transactions_root(),
                                block.transactions_root(),
                            )),
                    );
                } else {
                    if let Some(metrics) = ckb_metrics::handle() {
                        metrics.ckb_relay_transaction_short_id_collide.inc();
                    }
                    return ReconstructionResult::Collided;
                }
```

**File:** sync/src/relayer/compact_block_process.rs (L26-33)
```rust
// Keeping in mind that short_ids are expected to occasionally collide.
// On receiving compact-block message,
// while the reconstructed the block has a different transactions_root,
// 1. if all the transactions are prefilled,
// the node should ban the peer but not mark the block invalid
// because of the block hash may be wrong.
// 2. otherwise, there may be short_id collision in transaction pool,
// the node retreat to request all the short_ids from the peer.
```

**File:** sync/src/relayer/compact_block_process.rs (L128-151)
```rust
            ReconstructionResult::Missing(transactions, uncles) => {
                let missing_transactions: Vec<u32> =
                    transactions.into_iter().map(|i| i as u32).collect();

                if let Some(metrics) = ckb_metrics::handle() {
                    metrics
                        .ckb_relay_cb_fresh_tx_cnt
                        .inc_by(missing_transactions.len() as u64);
                    metrics.ckb_relay_cb_reconstruct_fail.inc();
                }

                let missing_uncles: Vec<u32> = uncles.into_iter().map(|i| i as u32).collect();
                missing_or_collided_post_process(
                    compact_block,
                    block_hash.clone(),
                    shared,
                    self.nc,
                    missing_transactions,
                    missing_uncles,
                    self.peer,
                )
                .await;

                StatusCode::CompactBlockRequiresFreshTransactions.with_context(&block_hash)
```

**File:** sync/src/relayer/compact_block_process.rs (L153-171)
```rust
            ReconstructionResult::Collided => {
                let missing_transactions: Vec<u32> = compact_block
                    .short_id_indexes()
                    .into_iter()
                    .map(|i| i as u32)
                    .collect();
                let missing_uncles: Vec<u32> = vec![];
                missing_or_collided_post_process(
                    compact_block,
                    block_hash.clone(),
                    shared,
                    self.nc,
                    missing_transactions,
                    missing_uncles,
                    self.peer,
                )
                .await;
                StatusCode::CompactBlockMeetsShortIdsCollision.with_context(&block_hash)
            }
```

**File:** sync/src/relayer/compact_block_verifier.rs (L5-7)
```rust
// we assume that all the short_ids and prefilled transactions
// should NOT collide with each other,
// because in the tx-pool, the node should use short_id as the key.
```

**File:** tx-pool/src/component/orphan.rs (L41-45)
```rust
#[derive(Default, Debug, Clone)]
pub(crate) struct OrphanPool {
    pub(crate) entries: HashMap<ProposalShortId, Entry>,
    pub(crate) by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>,
}
```

**File:** tx-pool/src/component/orphan.rs (L134-158)
```rust
    pub fn add_orphan_tx(
        &mut self,
        tx: TransactionView,
        peer: PeerIndex,
        declared_cycle: Cycle,
    ) -> Vec<Byte32> {
        if self.entries.contains_key(&tx.proposal_short_id()) {
            return vec![];
        }

        debug!("add_orphan_tx {}", tx.hash());
        self.entries.insert(
            tx.proposal_short_id(),
            Entry::new(tx.clone(), peer, declared_cycle),
        );

        for out_point in tx.input_pts_iter() {
            self.by_out_point
                .entry(out_point)
                .or_default()
                .insert(tx.proposal_short_id());
        }

        // DoS prevention: do not allow OrphanPool to grow unbounded
        self.limit_size()
```

**File:** tx-pool/src/util.rs (L20-26)
```rust
pub(crate) fn check_txid_collision(tx_pool: &TxPool, tx: &TransactionView) -> Result<(), Reject> {
    let short_id = tx.proposal_short_id();
    if tx_pool.contains_proposal_id(&short_id) {
        return Err(Reject::Duplicated(tx.hash()));
    }
    Ok(())
}
```
