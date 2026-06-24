All claims verified against the actual code. Here is the validated output:

---

Audit Report

## Title
Shared `VerifyQueue` Exhaustion via Fee-less Transactions Blocks Legitimate Transaction Submissions — (File: tx-pool/src/component/verify_queue.rs)

## Summary
The `VerifyQueue` is a single shared pre-verification buffer capped at 256 MB (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE = 256_000_000`). P2P-relayed transactions are admitted after only non-contextual (format/size/cellbase) checks; fee validation is deferred until a `VerifyMgr` worker dequeues the entry and calls `_process_tx` → `pre_check` → `check_tx_fee`. An unprivileged P2P peer can flood the queue with structurally valid but fee-less transactions, keeping it perpetually saturated and causing all subsequent legitimate submissions to receive `Reject::Full`.

## Finding Description

**Admission guard — size only, no fee check**

`verify_queue.rs` L18 defines the cap:
```rust
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```
`VerifyQueue::is_full` at L104–106 is the sole admission guard:
```rust
pub fn is_full(&self, add_tx_size: usize) -> bool {
    add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
}
```
`add_tx` at L215–219 immediately returns `Reject::Full` when this fires, with no further processing.

**P2P relay path — fee check deferred past admission**

`submit_remote_tx` (process.rs L371–379) calls `resumeble_process_tx_and_notify_full_reject`, which calls `resumeble_process_tx` (L335–353). That function performs only `non_contextual_verify` (format, size ≤ `TRANSACTION_SIZE_LIMIT`, non-cellbase) and duplicate checks before calling `enqueue_verify_queue` — no fee or cell-existence check at admission.

`non_contextual_verify` (util.rs L56–83) confirms: it runs `NonContextualTransactionVerifier`, checks `TRANSACTION_SIZE_LIMIT`, and rejects cellbase transactions. Fee and cell resolution are absent.

Fee validation (`check_tx_fee`, util.rs L28–54) is called inside `pre_check` (process.rs L269–316) via `resolve_tx` + `check_tx_fee`. `pre_check` is invoked by `_process_tx` (process.rs L715), which is called by `VerifyMgr` workers only **after** dequeuing (verify_mgr.rs L147–154). The queue slot is already consumed at that point.

**Existing checks shown insufficient**

- `non_contextual_verify` explicitly excludes fee and cell-existence checks (util.rs L56–83).
- There is no per-peer admission quota in `add_tx` or `enqueue_verify_queue`.
- `ban_malformed` (process.rs L679–703) is only triggered when `reject.is_malformed_tx()` returns true (process.rs L514). `Reject::LowFeeRate` is not a malformed rejection, so the attacker is never banned and `remove_txs_by_peer` is never called for fee-less submissions.

**Attack path**

1. Attacker opens P2P connections to the target node.
2. Attacker crafts transactions passing `non_contextual_verify`: structurally valid, ≤ 512 KB, at least one input/output, non-cellbase — but with zero fees and inputs referencing non-existent outpoints.
3. Each transaction is admitted into `VerifyQueue` without fee or cell-existence checks.
4. ~512 maximum-size transactions (512 × 512 KB ≈ 256 MB) saturate the queue.
5. `VerifyMgr` workers dequeue and reject them (fee check fails at `pre_check`), but the attacker re-submits faster than workers drain, keeping the queue full.
6. All legitimate `send_transaction` RPC calls and P2P relay submissions receive `Reject::Full`.

## Impact Explanation
A sustained P2P flood keeps the shared `VerifyQueue` at capacity, making the node unable to accept any new transaction from RPC or relay for the duration of the attack. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** The attacker pays no on-chain fees; cost is bandwidth only.

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