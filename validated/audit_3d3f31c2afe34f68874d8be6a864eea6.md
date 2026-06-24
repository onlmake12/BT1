Audit Report

## Title
Global `VerifyQueue` Has No Per-Peer Byte Quota, Allowing Any P2P Peer to Saturate It and Block All Transaction Submissions — (File: tx-pool/src/component/verify_queue.rs)

## Summary
`VerifyQueue` enforces a single global 256 MB ceiling (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`) with no per-peer byte accounting. Any unprivileged P2P peer can fill this queue by completing the standard `RelayTransactionHashes` → `GetRelayTransactions` → `RelayTransactions` flow with structurally-valid but contextually-invalid transactions. Once the ceiling is reached, every subsequent `add_tx` call from any peer or RPC caller returns `Reject::Full`, rendering the node's transaction pool non-functional for as long as the attacker sustains the submission rate.

## Finding Description
**Root cause — global-only fullness check with no per-peer quota:**

`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE` is hard-coded at 256 MB:
```rust
// tx-pool/src/component/verify_queue.rs L17-18
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```
`is_full` compares only the global `total_tx_size` counter against this constant; the `VerifyQueue` struct contains no `HashMap<PeerIndex, usize>` or any per-peer tracking field:
```rust
// L103-106
pub fn is_full(&self, add_tx_size: usize) -> bool {
    add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
}
```
When `is_full` returns `true`, `add_tx` immediately rejects every caller regardless of origin:
```rust
// L215-219
if self.is_full(tx_size) {
    return Err(Reject::Full(format!(
        "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
        tx.hash()
    )));
}
```

**Exploit path — pull-based relay protocol does not prevent the attack:**

`TransactionsProcess::execute` (`sync/src/relayer/transactions_process.rs` L37-96) filters incoming transactions to only those the node previously requested via `GetRelayTransactions`. This adds one round trip but does not prevent the attack: the attacker announces hashes via `RelayTransactionHashes`, waits for the node's `GetRelayTransactions` request, then delivers structurally-valid transactions referencing non-existent inputs. Each passes `non_contextual_verify` (no input-existence check), is enqueued via `submit_remote_tx` → `resumeble_process_tx` → `enqueue_verify_queue`, and increments `total_tx_size` toward 256 MB.

**Why existing guards are insufficient:**

- `remove_txs_by_peer` (`verify_queue.rs` L159-168) purges a peer's entries only on disconnect; it provides no protection while the peer remains connected.
- The relay rate limiter is per-message, not per-byte; a single `RelayTransactionHashes` message can carry thousands of hashes, so the rate limiter does not bound byte volume injected into the queue.
- `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32767` is far above the number of transactions needed to fill the queue.
- Workers drain the queue asynchronously and reject the attacker's transactions at the resolve step, but the attacker immediately refills freed space, sustaining the saturated state indefinitely.

The `Reject::Full` error propagates to the RPC layer as `PoolIsFull (-1106)`, blocking both P2P-relayed and RPC-submitted transactions from all users.

## Impact Explanation
While the queue is saturated, the node cannot accept any new transaction from any source. This constitutes a sustained, low-cost denial-of-service against a CKB node's transaction pool. This matches **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node** (the tx-pool is rendered non-functional). If applied simultaneously to multiple nodes, it also matches **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
- **Entry requirement**: Any peer completing a standard P2P handshake can use the `RelayTransactionHashes` → `GetRelayTransactions` → `RelayTransactions` flow. No privilege is required.
- **Cost**: Non-contextual verification does not check input existence or fee sufficiency. The attacker crafts structurally-valid transactions referencing non-existent inputs; these pass the structural gate and occupy queue space until workers reject them at the resolve step. Cost is approximately 256 MB of bandwidth per fill cycle.
- **Sustainability**: As workers drain entries and free space, the attacker announces new hashes and refills. The attack loop is cheap and continuous.
- **No per-peer quota**: Nothing in `VerifyQueue` or the relay handler limits how many bytes a single peer may contribute to the global queue.

## Recommendation
**Short term:** Introduce a per-peer byte quota inside `VerifyQueue`. Add a `HashMap<PeerIndex, usize>` tracking each peer's contribution to `total_tx_size`, analogous to how `remove_txs_by_peer` already tracks peer ownership. Reject `add_tx` calls that would exceed the per-peer cap.

**Long term:** Apply byte-volume rate limiting at the relay-protocol layer (e.g., in `TransactionsProcess::execute`) so that a single peer cannot inject bytes faster than workers can drain them. Regularly review `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE` relative to `max_tx_verify_cycles` and worker count to ensure the queue cannot be held full by a realistic number of peers.

## Proof of Concept
1. Connect to a target CKB node as a standard P2P peer.
2. Send a `RelayTransactionHashes` message containing 512 unique transaction hashes.
3. Wait for the node to respond with `GetRelayTransactions` requesting those hashes.
4. Respond with a `RelayTransactions` message containing 512 transactions of ~512 KB each that are structurally valid (pass `NonContextualTransactionVerifier`) but reference non-existent input cells.
5. Each transaction passes `non_contextual_verify`, is inserted into `VerifyQueue` via `resumeble_process_tx`, and increments `total_tx_size` toward 256 MB.
6. Once `total_tx_size ≥ DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`, every subsequent `add_tx` call returns `Reject::Full`. All RPC `send_transaction` calls and all P2P-relayed transactions from legitimate users are rejected.
7. As workers drain the queue (rejecting the attacker's transactions at the resolve step), repeat steps 2–4 with fresh hashes to keep `total_tx_size` at the ceiling.

The existing unit test `submit_remote_tx_notifies_relayer_when_verify_queue_is_full` (`tx-pool/src/component/tests/chunk.rs` L376-402) directly confirms the `Reject::Full` behavior when `total_tx_size` reaches the limit, validating the core mechanism of this attack.