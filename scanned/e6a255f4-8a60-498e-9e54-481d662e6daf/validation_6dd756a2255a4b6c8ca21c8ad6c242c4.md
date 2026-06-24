Audit Report

## Title
Global `VerifyQueue` Lacks Per-Peer Quota, Allowing Any P2P Peer to Block All Transaction Submissions — (File: tx-pool/src/component/verify_queue.rs)

## Summary
The `VerifyQueue` in CKB's tx-pool uses a single global 256 MB ceiling (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`) with no per-peer byte quota. Any unprivileged P2P peer can relay ~500 structurally valid but contextually invalid transactions (referencing non-existent inputs) to saturate the queue, causing every subsequent `add_tx` call — from any source — to return `Reject::Full`. The node's transaction pool is effectively disabled for as long as the attacker maintains the submission rate.

## Finding Description
`VerifyQueue` (`tx-pool/src/component/verify_queue.rs`, L17-18) defines a hard-coded global ceiling:
```rust
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```
The fullness check at L103-106 is purely global — it compares `total_tx_size` against this constant with no per-peer accounting:
```rust
pub fn is_full(&self, add_tx_size: usize) -> bool {
    add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
}
```
When `is_full` returns `true`, `add_tx` (L215-219) immediately returns `Reject::Full` for every caller regardless of origin.

The entry path for a remote peer is `submit_remote_tx` (`process.rs` L371-379) → `resumeble_process_tx_and_notify_full_reject` → `resumeble_process_tx` (L335-353). Before enqueuing, only `non_contextual_verify` is called (`util.rs` L56-83), which runs `NonContextualTransactionVerifier` (structural checks), enforces the 512 KB size limit, and rejects cellbase transactions. It does **not** check input existence or fee sufficiency. Transactions referencing non-existent inputs pass this gate and are inserted into the queue. Contextual resolution (which would reject them) happens later inside the worker pool.

`remove_txs_by_peer` (`verify_queue.rs` L158-168) can purge a peer's entries on disconnect, but provides no protection while the peer remains connected and actively refilling the queue.

With `TRANSACTION_SIZE_LIMIT = 512 * 1_000` bytes (`util/types/src/core/tx_pool.rs` L309), filling the 256 MB queue requires approximately 500 transactions — a trivially small number.

## Impact Explanation
This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** While the immediate effect is on a single node's transaction pool (all `send_transaction` RPC calls and P2P-relayed transactions are rejected), an attacker targeting multiple nodes simultaneously — or targeting well-connected relay nodes — can cause sustained network-wide transaction relay congestion. The cost is bandwidth alone; no CKB tokens, mining power, or privileged access are required.

## Likelihood Explanation
- **Entry path**: Any peer completing a standard P2P handshake can relay transactions via `RelayTransactions` — no privilege required.
- **Cost**: Non-contextual verification does not check input existence or fee sufficiency. Crafting structurally valid transactions referencing non-existent inputs is trivial and free.
- **Sustainability**: As workers drain and reject the attacker's transactions, the attacker immediately resubmits a fresh batch. The loop is cheap and continuous.
- **No per-peer quota**: Nothing in `VerifyQueue` or the relay handler limits how many bytes a single peer may contribute to `total_tx_size`.

## Recommendation
**Short term:** Introduce a per-peer byte quota inside `VerifyQueue`. Track each `PeerIndex`'s contribution to `total_tx_size` (the data structure for `remove_txs_by_peer` already tracks peer ownership) and reject additions that would exceed the per-peer cap.

**Long term:** Apply rate limiting at the relay-protocol layer (e.g., in the `RelayTransactions` handler) so a single peer cannot submit transactions faster than workers can drain them. Periodically review `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE` relative to `max_tx_verify_cycles` and worker count.

## Proof of Concept
1. Connect to a target CKB node as a standard P2P peer.
2. Craft ~500 transactions of ~512 KB each that are structurally valid (pass `NonContextualTransactionVerifier`) but reference non-existent input cells.
3. Relay all transactions via `RelayTransactions` messages. Each passes `non_contextual_verify` and is inserted into the `VerifyQueue`, incrementing `total_tx_size` toward 256 MB.
4. Once `total_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`, every subsequent `add_tx` call returns `Reject::Full`. All RPC `send_transaction` calls and all P2P-relayed transactions from legitimate users are rejected.
5. As workers drain the queue (rejecting the attacker's transactions at the resolve step), immediately relay a new batch to keep `total_tx_size` at the ceiling.
6. The existing unit test `submit_remote_tx_notifies_relayer_when_verify_queue_is_full` (`tx-pool/src/component/tests/chunk.rs` L376-402) already demonstrates the `Reject::Full` behavior when `total_tx_size` is set to `256_000_000 - 1`, confirming the trigger condition.