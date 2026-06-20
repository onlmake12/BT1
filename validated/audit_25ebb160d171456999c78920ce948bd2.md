### Title
Blocking Send on Bounded `tx_relay_sender` Channel Inside Async Tokio Task Causes Worker Thread Starvation — (`tx-pool/src/process.rs`)

---

### Summary

`TxPoolService::send_result_to_relayer` performs a **blocking** crossbeam `send()` on a bounded `tx_relay_sender` channel from within async tokio tasks. If the channel's consumer (the relayer) is slow or absent, the channel fills up and every subsequent call to `send_result_to_relayer` blocks a tokio worker thread indefinitely. An unprivileged peer or RPC caller can trigger this by flooding the node with transactions that produce relay-eligible rejections, exhausting the tokio thread pool and causing a liveness failure across the entire tx-pool service.

---

### Finding Description

`TxPoolService` holds a `ckb_channel::Sender<TxVerificationResult>` field named `tx_relay_sender`. [1](#0-0) 

`ckb_channel` re-exports `crossbeam_channel::bounded`, whose `Sender::send()` **blocks** the calling thread until capacity is available. [2](#0-1) 

The helper `send_result_to_relayer` wraps this blocking call with no timeout, no `try_send`, and no `spawn_blocking` guard: [3](#0-2) 

This function is called **12 times** throughout `tx-pool/src/process.rs` from within `async` methods of `TxPoolService` (e.g., `process_orphan_tx`, `_process_tx`, and the reject callback path). A second blocking send site exists in the registered reject callback: [4](#0-3) 

The channel's only consumer is `send_bulk_of_tx_hashes` in the relayer, which drains results via `take_relay_tx_verify_results` on a **periodic timer**: [5](#0-4) 

The channel is bounded (size 16 in the test harness that mirrors production construction): [6](#0-5) 

When the relayer timer fires infrequently, or when the node has no connected relay peers, the consumer stalls. Once the 16-slot buffer is full, every call to `send_result_to_relayer` from an async tokio task **parks the tokio worker thread** rather than yielding it. With enough concurrent rejections, all worker threads in the tokio pool become blocked, and the entire tx-pool service — including block assembly, RPC responses, and chain reorg handling — becomes unresponsive.

---

### Impact Explanation

- **Tx-pool liveness failure / node DoS**: the tokio runtime that drives `TxPoolService` is starved; no async tasks make progress.
- Block template requests (`get_block_template`) stall, breaking mining.
- Chain reorg notifications stall, breaking sync.
- All RPC calls that route through the tx-pool controller hang indefinitely.

---

### Likelihood Explanation

An unprivileged peer can submit transactions over P2P relay (`SubmitRemoteTx`) or an RPC caller can use `send_transaction`. Transactions rejected with `is_allowed_relay() == true` (e.g., fee-too-low, capacity overflow, duplicate) each enqueue one item. Sending 17+ such transactions faster than the relayer timer drains them is trivially achievable. No special privilege, key, or majority hashpower is required.

---

### Recommendation

Replace the blocking `send()` in `send_result_to_relayer` with a non-blocking `try_send()`, discarding or logging overflow silently (relay results are best-effort). Alternatively, wrap the call in `tokio::task::spawn_blocking` or use an unbounded channel for this notification path, since relay result delivery is not a consensus-critical operation and dropping overflow events is safe.

```rust
// tx-pool/src/process.rs
pub(crate) fn send_result_to_relayer(&self, result: TxVerificationResult) {
    if let Err(e) = self.tx_relay_sender.try_send(result) {
        // Channel full or disconnected — relay notification is best-effort
        debug!("tx-pool tx_relay_sender dropped: {}", e);
    }
}
```

The same fix applies to the blocking send in the reject callback in `shared/src/shared_builder.rs`.

---

### Proof of Concept

1. Start a CKB node with no outbound relay peers (so the relayer timer never drains `tx_relay_sender`).
2. Submit 17+ transactions via RPC `send_transaction` or P2P relay that are rejected with `is_allowed_relay() == true` (e.g., transactions with fee below minimum).
3. Each rejection calls `send_result_to_relayer` → `tx_relay_sender.send(...)` (blocking crossbeam send).
4. After 16 items fill the buffer, the 17th call blocks the tokio worker thread.
5. Repeat until all tokio worker threads are blocked.
6. Observe: subsequent RPC calls (e.g., `get_block_template`, `get_transaction`) hang indefinitely; the node is effectively dead.

### Citations

**File:** tx-pool/src/service.rs (L749-749)
```rust
    pub(crate) tx_relay_sender: ckb_channel::Sender<TxVerificationResult>,
```

**File:** util/channel/src/lib.rs (L1-5)
```rust
//! Reexports `crossbeam_channel` to uniform the dependency version.
pub use crossbeam_channel::{
    Receiver, RecvError, RecvTimeoutError, Select, SendError, Sender, TrySendError, after, bounded,
    select, tick, unbounded,
};
```

**File:** tx-pool/src/process.rs (L673-677)
```rust
    pub(crate) fn send_result_to_relayer(&self, result: TxVerificationResult) {
        if let Err(e) = self.tx_relay_sender.send(result) {
            error!("tx-pool tx_relay_sender internal error {}", e);
        }
    }
```

**File:** shared/src/shared_builder.rs (L587-593)
```rust
            if reject.is_allowed_relay()
                && let Err(e) = tx_relay_sender.send(TxVerificationResult::Reject {
                    tx_hash: tx_hash.clone(),
                })
            {
                error!("tx-pool tx_relay_sender internal error {}", e);
            }
```

**File:** sync/src/relayer/mod.rs (L639-642)
```rust
        let tx_verify_results = self
            .shared
            .state()
            .take_relay_tx_verify_results(MAX_RELAY_TXS_NUM_PER_BATCH);
```

**File:** tx-pool/src/component/tests/chunk.rs (L325-325)
```rust
    let (tx_relay_sender, tx_relay_receiver) = ckb_channel::bounded(16);
```
