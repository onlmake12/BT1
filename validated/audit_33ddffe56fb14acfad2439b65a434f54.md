Audit Report

## Title
VerifyQueue Hard-Cap Rejection With No Fee-Rate Eviction Enables Cheap Network-Wide DoS — (File: `tx-pool/src/component/verify_queue.rs`)

## Summary
`VerifyQueue` enforces a hard 256 MB total-size cap (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE = 256_000_000`) with no fee-rate-based eviction. Once an attacker fills the queue with minimum-fee transactions, every subsequent `add_tx` call returns `Reject::Full` regardless of the incoming transaction's fee rate, blocking both P2P relay (`submit_remote_tx`) and miner proposal (`notify_tx`) paths for all users. The main pool's `limit_size` eviction mechanism is never reached because transactions never exit the verify queue stage.

## Finding Description
`VerifyQueue.is_full` at `tx-pool/src/component/verify_queue.rs` L104–106 performs a pure size comparison with no fee-rate awareness:

```rust
pub fn is_full(&self, add_tx_size: usize) -> bool {
    add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
}
```

When it returns `true`, `add_tx` at L215–220 immediately rejects the incoming transaction with no attempt to evict a lower-fee-rate incumbent:

```rust
if self.is_full(tx_size) {
    return Err(Reject::Full(format!(
        "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
        tx.hash()
    )));
}
```

The `VerifyQueue` struct exposes only `remove_tx`, `remove_txs`, `remove_txs_by_peer`, `pop_front`, and `clear` — there is no `limit_size` or fee-rate-ordered eviction path. By contrast, the main pool's `limit_size` at `tx-pool/src/pool.rs` L292–328 evicts the lowest-fee-rate entry in a loop until the pool fits within its configured limit. Transactions in the verify queue never reach this eviction logic.

Both `submit_remote_tx` (P2P relay) and `notify_tx` (miner proposal) funnel through `resumeble_process_tx_and_notify_full_reject` → `resumeble_process_tx` → `enqueue_verify_queue` → `add_tx` at `tx-pool/src/process.rs` L355–384. There are no per-peer submission limits in the tx-pool code, so a single attacker can fill the entire 256 MB queue unimpeded.

## Impact Explanation
Once the verify queue is saturated, every `submit_remote_tx` and `notify_tx` call from every peer and every RPC caller returns `Reject::Full`. Legitimate high-fee transactions cannot displace attacker entries because there is no eviction path. The attacker can sustain the condition indefinitely by resubmitting as workers drain entries. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation
The entry path is fully unprivileged — any P2P peer or RPC caller can submit transactions. `TRANSACTION_SIZE_LIMIT` is enforced in `tx-pool/src/util.rs` L68; filling 256 MB requires only ~512 maximum-size transactions. At the default minimum fee rate of 1,000 shannons/KB, the total attack cost is approximately 2–3 CKB. The attacker can declare cycles above `max_tx_verify_cycles` to route transactions to the slower large-cycle worker path, extending the denial window. The attack is repeatable and requires no special privileges or victim mistakes.

## Recommendation
**Short term**: In `add_tx`, when `is_full` returns `true`, compare the incoming transaction's fee rate against the lowest-fee-rate entry currently in the queue. If the incoming fee rate is higher, evict the lowest entry and admit the new one, mirroring the main pool's `limit_size` logic.

**Long term**: Introduce a `min_verify_queue_fee_rate` floor that rises dynamically as the queue fills, mirroring the main pool's dynamic minimum fee rate, so the verify queue cannot be cheaply saturated by minimum-fee spam.

## Proof of Concept
The existing unit tests at `tx-pool/src/component/tests/chunk.rs` L376–402 and L404–428 explicitly confirm the behavior for both `submit_remote_tx` and `notify_tx`: setting `total_tx_size` to `256_000_000 - 1` and submitting any transaction immediately returns `Err(Reject::Full(_))`.

Manual reproduction steps:
1. Obtain ~3 CKB to fund ~512 transactions each consuming ~512 KB of serialized size.
2. Set each transaction's fee to exactly the `min_fee_rate` threshold (1,000 shannons/KB).
3. Optionally declare cycles as `max_tx_verify_cycles + 1` to route to the slow large-cycle worker.
4. Submit all transactions via `send_transaction` RPC or P2P relay.
5. After ~512 submissions, `verify_queue.total_tx_size` reaches 256 MB.
6. Every subsequent `send_transaction` from any user returns `PoolIsFull (-1106)`; every `notify_tx` from miners also returns `Reject::Full`.
7. Resubmit new transactions as workers drain old ones to sustain the DoS indefinitely.