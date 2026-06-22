### Title
`store_snapshot()` Without Guaranteed `update_tx_pool_for_reorg()` Leads to Persistent Tx-Pool/Chain Inconsistency - (File: `chain/src/verify.rs`)

---

### Summary

In `chain/src/verify.rs`, when a new best block is accepted, `store_snapshot()` atomically publishes the new chain tip to all readers, but the mandatory follow-up `update_tx_pool_for_reorg()` is dispatched via a non-blocking `try_send()` on a bounded channel. If that send fails (channel full), the error is silently logged and execution continues. The result is a persistent split: the global `Snapshot` reflects the new tip while the tx-pool's internal snapshot still points to the old tip, exactly mirroring the FantiumClaiming pattern where `takeClaimingSnapshot()` mutates shared state without completing the required follow-up call.

---

### Finding Description

After the DB transaction for a new best block is committed, `verify_block` executes the following sequence:

```
store_snapshot(new_snapshot)          // line 383 – global snapshot atomically advanced
update_tx_pool_for_reorg(...)         // lines 387-394 – try_send; may silently drop
``` [1](#0-0) 

`update_tx_pool_for_reorg` on the controller side uses `try_send` on the bounded `reorg_sender` channel: [2](#0-1) 

If `try_send` returns an error (channel full), the call site in `verify_block` only logs the error and returns `Ok(true)` — the block is accepted, the global snapshot is advanced, but the tx-pool reorg task is never queued: [3](#0-2) 

Inside the reorg task, the very first thing `_update_tx_pool_for_reorg` does is overwrite `tx_pool.snapshot` with the new snapshot: [4](#0-3) 

When the reorg message is dropped, this assignment never happens. The tx-pool's `snapshot` field remains pinned to the pre-reorg tip while `Shared::snapshot()` already returns the post-reorg tip. Every subsequent tx-pool operation — admission checks, `submit_entry`, block-template assembly — reads from the stale snapshot.

The `reorg_sender` channel is bounded (`DEFAULT_CHANNEL_SIZE = 512`): [5](#0-4) 

The reorg consumer loop processes one message at a time and holds the tx-pool write lock for the full duration of `update_tx_pool_for_reorg` (which re-validates and re-classifies every retained transaction). Under a sustained block flood or a deep reorg with a large mempool, the consumer can fall behind and the channel fills.

---

### Impact Explanation

Once the tx-pool snapshot diverges from the chain snapshot:

1. **Double-spend persistence** — transactions spending outputs that were consumed in the newly attached blocks are not removed by `remove_committed_txs`; they remain in the pending pool and can be re-broadcast or included in a miner's next template.
2. **Invalid block templates** — `get_block_template` assembles transactions against the stale snapshot. Outputs it believes are live may already be spent; the resulting block will fail contextual verification on any peer.
3. **Proposal-window corruption** — `remove_by_detached_proposal` is never called for the dropped reorg, so proposal short-IDs that should have been re-classified remain in the wrong pool bucket, causing miners to omit or double-propose transactions.
4. **Cascading stale state** — every subsequent `update_tx_pool_for_reorg` that *does* succeed will compute diffs against the wrong baseline, compounding the inconsistency.

---

### Likelihood Explanation

The trigger is reachable by any unprivileged P2P block relayer:

- A peer (or a set of peers) that rapidly relays a sequence of valid blocks — each one becoming the new best tip — queues one reorg message per block.
- The reorg consumer holds the tx-pool write lock while re-verifying retained transactions. With a large mempool (thousands of entries) or a deep reorg, each message can take hundreds of milliseconds.
- At 512 messages × (processing time per message), the channel saturates. Any block arriving while the channel is full silently drops its reorg notification.
- No special privilege is required: block relay is the standard P2P path open to every peer.

---

### Recommendation

1. **Replace `try_send` with an unbounded channel** for the reorg path, or use a blocking `send` so that `verify_block` waits until the reorg is queued. The reorg channel is a low-volume, high-importance path; backpressure here is correct.
2. **Propagate the error** from `update_tx_pool_for_reorg` up through `verify_block` so that a failed enqueue causes the block-processing pipeline to stall rather than silently diverge.
3. **Mirror the `truncate` pattern** — the internal `truncate` function in `chain/src/verify.rs` (lines 867–907) also calls `store_snapshot` without notifying the tx-pool, delegating that responsibility entirely to the caller: [6](#0-5) 

The RPC handler compensates with `clear_pool`, but the internal function itself is a latent inconsistency source. Both sites should enforce the invariant that a snapshot update is always paired with a tx-pool update.

---

### Proof of Concept

1. Connect a peer to a CKB node that has a large mempool (e.g., thousands of pending transactions).
2. Rapidly relay a long sequence of valid blocks (each extending the previous tip) so that each accepted block enqueues a reorg message before the consumer finishes processing the previous one.
3. Once the `reorg_sender` channel reaches capacity (512 entries), the next `try_send` returns `TrySendError::Full`; `verify_block` logs the error and returns `Ok(true)`.
4. The global snapshot now points to the new tip; `tx_pool.snapshot` still points to the old tip.
5. Call `get_block_template` — the returned template will contain transactions that were already committed in the newly attached blocks, producing a block that any validating peer will reject.
6. Call `get_transaction` on a tx that was committed in the new blocks — the tx-pool will still report it as `pending` rather than `committed`.

### Citations

**File:** chain/src/verify.rs (L379-398)
```rust
            let new_snapshot =
                self.shared
                    .new_snapshot(tip_header, cannon_total_difficulty, epoch, new_proposals);

            self.shared.store_snapshot(Arc::clone(&new_snapshot));

            let tx_pool_controller = self.shared.tx_pool_controller();
            if tx_pool_controller.service_started() {
                if let Err(e) = tx_pool_controller.update_tx_pool_for_reorg(
                    fork.detached_blocks().clone(),
                    fork.attached_blocks().clone(),
                    fork.detached_proposal_id().clone(),
                    new_snapshot,
                ) {
                    error!("[verify block] notify update_tx_pool_for_reorg error {}", e);
                }
                if let Err(e) = tx_pool_controller.update_ibd_state(in_ibd) {
                    error!("Notify update_ibd_state error {}", e);
                }
            }
```

**File:** chain/src/verify.rs (L897-906)
```rust
        let new_snapshot = self.shared.new_snapshot(
            target_tip_header,
            target_block_ext.total_difficulty,
            target_epoch_ext,
            new_proposals,
        );

        self.shared.store_snapshot(Arc::clone(&new_snapshot));

        Ok(())
```

**File:** tx-pool/src/service.rs (L53-54)
```rust
pub(crate) const DEFAULT_CHANNEL_SIZE: usize = 512;
pub(crate) const BLOCK_ASSEMBLER_CHANNEL_SIZE: usize = 100;
```

**File:** tx-pool/src/service.rs (L241-258)
```rust
    pub fn update_tx_pool_for_reorg(
        &self,
        detached_blocks: VecDeque<BlockView>,
        attached_blocks: VecDeque<BlockView>,
        detached_proposal_id: HashSet<ProposalShortId>,
        snapshot: Arc<Snapshot>,
    ) -> Result<(), AnyError> {
        let notify = Notify::new((
            detached_blocks,
            attached_blocks,
            detached_proposal_id,
            snapshot,
        ));
        self.reorg_sender.try_send(notify).map_err(|e| {
            let (_m, e) = handle_try_send_error(e);
            e.into()
        })
    }
```

**File:** tx-pool/src/process.rs (L1048-1056)
```rust
    tx_pool.snapshot = Arc::clone(&snapshot);

    // NOTE: `remove_by_detached_proposal` will try to re-put the given expired/detached proposals into
    // pending-pool if they can be found within txpool. As for a transaction
    // which is both expired and committed at the one time(commit at its end of commit-window),
    // we should treat it as a committed and not re-put into pending-pool. So we should ensure
    // that involves `remove_committed_txs` before `remove_expired`.
    tx_pool.remove_committed_txs(attached.iter(), callbacks, detached_headers);
    tx_pool.remove_by_detached_proposal(detached_proposal_id.iter());
```
