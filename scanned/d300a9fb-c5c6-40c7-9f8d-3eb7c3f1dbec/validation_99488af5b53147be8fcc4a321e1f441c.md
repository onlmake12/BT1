Looking at the actual code to validate each claim in the report.

Audit Report

## Title
Shared `VerifyQueue` Exhaustion via Fee-less Transactions Blocks Legitimate Transaction Submissions — (File: tx-pool/src/component/verify_queue.rs)

## Summary
The `VerifyQueue` is a single shared pre-verification buffer with a hardcoded 256 MB size cap. For the P2P relay path, transactions are admitted into the queue after only non-contextual (format) checks — fee validation is deferred until after dequeue by a `VerifyMgr` worker. An unprivileged P2P peer can flood the queue with structurally valid but fee-less transactions, keeping it perpetually saturated and causing all subsequent legitimate submissions to be rejected with `Reject::Full`.

## Finding Description

**Admission guard — size only, no fee check**

`VerifyQueue::is_full` is the sole admission guard:

```rust
// verify_queue.rs L17-18
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;

// verify_queue.rs L103-106
pub fn is_full(&self, add_tx_size: usize) -> bool {
    add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
}
```

When full, `add_tx` immediately returns `Reject::Full` (L215-220) with no further processing.

**P2P relay path — fee check deferred past admission**

`submit_remote_tx` (process.rs L371-379) calls `resumeble_process_tx`, which performs only `non_contextual_verify` before calling `enqueue_verify_queue`:

```rust
// process.rs L335-353
pub(crate) async fn resumeble_process_tx(...) -> Result<bool, Reject> {
    self.non_contextual_verify(&tx, remote).await?;   // format only
    // ... duplicate checks ...
    self.enqueue_verify_queue(tx, is_proposal_tx, remote).await  // admitted here
}
```

`non_contextual_verify` (util.rs L56-83) checks structure, size limit (`TRANSACTION_SIZE_LIMIT = 512 KB`), and cellbase status — it does **not** check fees or cell existence.

Fee validation (`check_tx_fee`) lives inside `pre_check` (process.rs L269-316), which is called by `_process_tx` only **after** a `VerifyMgr` worker dequeues the entry (verify_mgr.rs L147-154). By that point the queue slot is already consumed.

**Attack path**

1. Attacker opens one or more P2P connections to the target node.
2. Attacker crafts transactions that pass `non_contextual_verify`: structurally valid, ≤ 512 KB, at least one input/output, non-cellbase — but with zero fees and inputs referencing non-existent or spent cells.
3. Each such transaction is admitted into the `VerifyQueue` without fee or cell-existence checks.
4. ~512 maximum-size transactions (512 × 512 KB ≈ 256 MB) saturate the queue.
5. `VerifyMgr` workers dequeue and reject them cheaply (fee check fails at `pre_check`), but the attacker re-submits faster than workers drain, keeping the queue full.
6. All legitimate `send_transaction` RPC calls and P2P relay submissions receive `Reject::Full` for the duration of the attack.

**Existing checks shown insufficient**

- `non_contextual_verify` explicitly excludes fee and cell-existence checks.
- There is no per-peer admission quota or rate limit enforced inside `enqueue_verify_queue` or `add_tx`.
- The `ban_malformed` path (process.rs L679-703) only triggers on `is_malformed_tx()` rejections; `Reject::LowFeeRate` and `Reject::Resolve` are not malformed, so the attacker is never banned.
- The `remove_txs_by_peer` helper exists but is only called on ban, which never fires for fee-less transactions.

## Impact Explanation
A sustained P2P flood keeps the shared `VerifyQueue` at capacity, making the node unable to accept any new transaction — from RPC or relay — for the duration of the attack. This directly matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** The attacker pays no on-chain fees; cost is bandwidth only.

## Likelihood Explanation
Any unprivileged P2P peer can trigger this. CKB nodes accept inbound P2P connections by default. The attacker needs only to generate structurally valid transactions (trivial with the public SDK) and sustain a submission rate sufficient to refill the queue as workers drain it. Because `pre_check` involves a chain-state lookup (`resolve_tx`) for each dequeued transaction, the drain rate is bounded by I/O latency, making it feasible to outpace draining with modest bandwidth. Multiple connections multiply throughput linearly. The attack is repeatable indefinitely at near-zero cost.

## Recommendation
1. **Admit with fee pre-screening**: Before calling `enqueue_verify_queue` in `resumeble_process_tx`, perform a lightweight fee-rate plausibility check (e.g., compare declared output capacity against input capacity using only the transaction's own fields, without chain-state access) to reject obviously fee-less transactions at admission time.
2. **Per-peer queue quota**: Track how many bytes each remote peer has in the `VerifyQueue` and cap it (e.g., `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE / max_peers`). Enforce this in `add_tx` alongside the global size check.
3. **Extend ban to fee rejections**: Treat repeated `Reject::LowFeeRate` from the same peer as a bannable offense after a threshold, and call `remove_txs_by_peer` on ban.
4. **Dynamic back-pressure**: When the queue exceeds a high-water mark, reject new remote submissions rather than only at the hard 256 MB ceiling.

## Proof of Concept

**Minimal manual steps:**

1. Build a CKB node from this repository and start it with default config.
2. Using the CKB SDK, generate ~512 transactions that:
   - Have one input referencing a non-existent outpoint (e.g., all-zero tx hash, index 0).
   - Have one output with capacity equal to input capacity (zero fee).
   - Are padded with witness data to approach 512 KB each.
   - Pass `NonContextualTransactionVerifier` (valid script hashes, proper structure).
3. Connect to the node as a P2P peer and relay all 512 transactions via the relay protocol (`RelayTransactions` message), declaring any cycle count.
4. Immediately attempt to submit a legitimate, fee-paying transaction via the `send_transaction` RPC.
5. **Expected result**: The RPC returns `Reject::Full("verify_queue total_tx_size exceeded …")`.
6. Observe that as workers drain and reject the attacker's transactions, re-submitting the batch restores the full condition, keeping the queue saturated indefinitely.

**Invariant/fuzz test plan**: Write a property test that concurrently enqueues maximum-size zero-fee entries via the `add_tx` path while a legitimate fee-paying transaction attempts admission; assert that `Reject::Full` is never returned for the legitimate transaction when the queue contains only attacker-controlled entries — this invariant currently fails.