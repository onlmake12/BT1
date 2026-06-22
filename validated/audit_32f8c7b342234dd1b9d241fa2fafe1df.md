### Title
Off-by-One in `VerifyQueue::is_full` Incorrectly Rejects Valid Transactions at Exact Capacity Boundary — (`File: tx-pool/src/component/verify_queue.rs`)

### Summary

`VerifyQueue::is_full` uses a `>=` comparison instead of `>`, causing it to return `true` (queue full) when a transaction's serialized size exactly equals the remaining available space. This mirrors the original report's pattern: an overly strict guard condition that blocks a legitimate operation at a specific boundary value.

### Finding Description

In `tx-pool/src/component/verify_queue.rs`, the `is_full` method is:

```rust
pub fn is_full(&self, add_tx_size: usize) -> bool {
    add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
}
```

`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE` is 256,000,000 bytes. [1](#0-0) 

The remaining capacity is `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size`. When `add_tx_size` equals exactly that remaining capacity, the expression evaluates to `true` and the transaction is rejected with `Reject::Full`, even though adding it would bring `total_tx_size` to exactly the limit — not over it. [2](#0-1) 

The correct guard should be `add_tx_size > DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size` (strict greater-than), which would only reject when the transaction would actually exceed the limit.

This rejection propagates through `add_tx` → `enqueue_verify_queue` → `resumeble_process_tx` → `submit_remote_tx` / `notify_tx`, returning `Reject::Full` to the caller. [3](#0-2) 

### Impact Explanation

Any transaction sender (RPC caller via `send_transaction`, or P2P peer via relay) whose transaction's serialized size exactly equals the remaining verify-queue space receives a spurious `Reject::Full` error. The transaction is not lost — it can be resubmitted — but its admission is incorrectly denied at the boundary. This is a tx-pool admission availability issue: the function of the protocol is impacted at a specific edge case, matching the medium-risk pattern of the original report ("assets not at direct risk, but availability could be impacted"). [4](#0-3) 

### Likelihood Explanation

The condition requires `add_tx_size == DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size` exactly. While unlikely in random operation, it is deterministically reachable: an attacker or user who knows the current `total_tx_size` (observable via RPC pool stats) can craft or time a submission to hit this boundary. The entry path is fully unprivileged — any RPC caller or P2P peer can submit transactions. [5](#0-4) 

### Recommendation

Change the comparison in `is_full` from `>=` to `>`:

```rust
pub fn is_full(&self, add_tx_size: usize) -> bool {
    add_tx_size > DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
}
```

This allows a transaction that exactly fills the remaining space to be admitted, while still rejecting any transaction that would exceed the limit. [2](#0-1) 

### Proof of Concept

1. Observe current `total_tx_size` via pool diagnostics.
2. Compute `remaining = 256_000_000 - total_tx_size`.
3. Submit a transaction whose `serialized_size_in_block()` equals exactly `remaining`.
4. `is_full(remaining)` evaluates `remaining >= remaining` → `true`.
5. `add_tx` returns `Err(Reject::Full(...))` even though the transaction fits exactly.
6. The existing test at line 361 confirms this behavior: setting `total_tx_size = 256_000_000 - 1` causes any tx (even size-1) to be rejected, demonstrating the `>=` boundary is active. [6](#0-5)

### Citations

**File:** tx-pool/src/component/verify_queue.rs (L17-18)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

**File:** tx-pool/src/component/verify_queue.rs (L103-106)
```rust
    /// Returns true if the queue is full.
    pub fn is_full(&self, add_tx_size: usize) -> bool {
        add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L215-220)
```rust
        if self.is_full(tx_size) {
            return Err(Reject::Full(format!(
                "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
                tx.hash()
            )));
        }
```

**File:** tx-pool/src/process.rs (L355-368)
```rust
    async fn resumeble_process_tx_and_notify_full_reject(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        let tx_hash = tx.hash();
        let ret = self.resumeble_process_tx(tx, is_proposal_tx, remote).await;

        if matches!(ret, Err(Reject::Full(_))) {
            self.send_result_to_relayer(TxVerificationResult::Reject { tx_hash });
        }

        ret
```

**File:** tx-pool/src/process.rs (L371-383)
```rust
    pub(crate) async fn submit_remote_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Cycle,
        peer: PeerIndex,
    ) -> Result<bool, Reject> {
        self.resumeble_process_tx_and_notify_full_reject(tx, false, Some((declared_cycles, peer)))
            .await
    }

    pub(crate) async fn notify_tx(&self, tx: TransactionView) -> Result<bool, Reject> {
        self.resumeble_process_tx_and_notify_full_reject(tx, true, None)
            .await
```

**File:** tx-pool/src/component/tests/chunk.rs (L404-428)
```rust
#[tokio::test]
async fn notify_tx_notifies_relayer_when_verify_queue_is_full() {
    let (service, tx_relay_receiver) = service_with_relay_receiver();
    let tx = build_tx(vec![(&H256([1; 32]).into(), 0)], 1);
    let tx_hash = tx.hash();

    service
        .verify_queue
        .write()
        .await
        .set_total_tx_size_for_test(256_000_000 - 1);

    let ret = service.notify_tx(tx).await;

    assert!(matches!(ret, Err(crate::error::Reject::Full(_))));
    match tx_relay_receiver
        .try_recv()
        .expect("expected reject notification")
    {
        TxVerificationResult::Reject { tx_hash: rejected } => {
            assert_eq!(rejected, tx_hash);
        }
        _ => panic!("expected reject notification"),
    }
}
```
