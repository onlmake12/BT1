Audit Report

## Title
Non-Blocking `try_send` for Reorg Notifications Can Silently Drop Updates, Leaving Tx-Pool Permanently Inconsistent - (`tx-pool/src/service.rs`, `chain/src/verify.rs`)

## Summary
After committing a block (including any reorg), `chain/src/verify.rs` calls `tx_pool_controller.update_tx_pool_for_reorg(...)`, which internally uses `reorg_sender.try_send(notify)` — a non-blocking send on a bounded channel of size 512. If the channel is full, the notification is silently discarded: the dropped message is bound to `_m` and never retried. The tx-pool never learns about the reorg and remains permanently inconsistent with the committed chain state until the node restarts.

## Finding Description
In `chain/src/verify.rs` at lines 387–394, after the snapshot is committed to the store, the chain service calls `update_tx_pool_for_reorg`. The error is logged but execution continues unconditionally — there is no retry and the chain state is already committed.

In `tx-pool/src/service.rs` at line 254, `reorg_sender.try_send(notify)` is the non-blocking call. On `TrySendError::Full`, `handle_try_send_error` (defined in `tx-pool/src/error.rs` lines 38–44) extracts the message into `_m` (discarded) and returns only the error string. The reorg notification is gone.

The channel is created at `tx-pool/src/service.rs:513` with `DEFAULT_CHANNEL_SIZE = 512` (line 53). The reorg receiver loop at lines 697–731 processes one reorg at a time sequentially. Each reorg invokes `readd_detached_tx` (`tx-pool/src/process.rs:878–914`), which runs full CKB-VM script execution per detached transaction — making each iteration potentially slow. If the receiver is busy and 512 new block-commit notifications arrive before it drains the channel, all subsequent `try_send` calls fail silently.

When a notification is dropped:
- `remove_committed_txs` is never called for attached blocks → already-committed transactions remain in the pending pool.
- `readd_detached_tx` is never called for detached blocks → transactions from orphaned blocks are permanently lost from the mempool.
- The tx-pool snapshot remains stale → all downstream operations (fee estimation, RBF, block assembly) operate on incorrect state.

No self-healing mechanism exists. The inconsistency persists until node restart.

## Impact Explanation
This is a suboptimal implementation of the CKB tx-pool state management mechanism. The tx-pool is a critical component of the node's state: it drives block template construction, transaction relay, and fee estimation. Permanent desynchronization between the committed chain state and the tx-pool state causes block templates to include already-committed transactions (producing invalid templates) and silently drops user transactions from the mempool. This matches the **Medium** impact category: *Suboptimal implementation of CKB state storage mechanism* (2001–10000 points).

## Likelihood Explanation
The trigger requires 512 reorg/block-commit notifications to queue while the reorg receiver is occupied. During normal operation (~8s block time), this is unlikely. However, during IBD catch-up (where blocks are committed at maximum throughput) or during a period of frequent short reorgs with a loaded tx-pool, the receiver can fall behind. An adversary with moderate hashpower can deliberately trigger repeated shallow reorgs to keep the receiver busy while the chain service continues committing blocks. The condition is reachable by an unprivileged block relayer.

## Recommendation
Replace `reorg_sender.try_send(notify)` with a blocking `reorg_sender.send(notify).await` (or equivalent) so the chain service applies backpressure rather than dropping notifications. If blocking the chain service is unacceptable, use an unbounded channel for reorg notifications (reorgs are bounded in practice by fork-choice rules and are rare relative to normal block production). At minimum, implement a retry loop with a timeout and a hard failure if the tx-pool cannot be updated, rather than silently continuing with a stale state.

## Proof of Concept
1. Start a CKB node with a tx-pool containing many pending transactions (to slow down `readd_detached_tx`).
2. Trigger rapid block production or repeated shallow reorgs so that `update_tx_pool_for_reorg` is called faster than the reorg receiver can drain the channel.
3. After 512 queued notifications, observe `[verify block] notify update_tx_pool_for_reorg error` logged at `chain/src/verify.rs:393`.
4. Query the tx-pool via RPC: transactions from detached blocks are absent; transactions from attached blocks are still present.
5. Call `get_block_template`: the returned template contains already-committed transactions, confirming the inconsistency.