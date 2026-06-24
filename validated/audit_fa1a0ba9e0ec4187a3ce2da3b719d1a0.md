Audit Report

## Title
Missing Count Limit on `RelayTransactions` Message Enables Lock-Contention DoS — (File: sync/src/relayer/transactions_process.rs)

## Summary
`TransactionsProcess::execute()` acquires two `parking_lot::Mutex` locks (`tx_filter` and `unknown_tx_hashes`) and iterates over every transaction in a peer-supplied `RelayTransactions` message with no count guard, computing a cryptographic hash for each entry while holding both locks. Every other relay message handler enforces `MAX_RELAY_TXS_NUM_PER_BATCH` (32,767) before touching shared state. An unprivileged peer can send a crafted `RelayTransactions` message containing far more transactions than were ever requested, holding both mutexes for an extended period and stalling relay-layer transaction propagation on the victim node.

## Finding Description
`TransactionsProcess::execute()` at `sync/src/relayer/transactions_process.rs` lines 39–57 acquires `tx_filter` (line 41) and `unknown_tx_hashes` (line 43) as `parking_lot::Mutex` guards, then immediately chains `.map(|tx| (tx.transaction().to_entity().into_view(), tx.cycles().into()))` before `.filter(...)`. Because `.map()` precedes `.filter()`, `to_entity().into_view()` — which performs hash computation and allocation — is called for **every** transaction in the message while both locks are held, regardless of whether the transaction passes the filter.

The function is synchronous (non-`async`), called directly from the async `try_process` dispatcher at `sync/src/relayer/mod.rs` line 137. Holding a `parking_lot::Mutex` inside an async context without yielding blocks the Tokio executor thread for the entire iteration.

All peer-facing relay handlers enforce the count limit before touching shared state:
- `TransactionHashesProcess::execute()` — `sync/src/relayer/transaction_hashes_process.rs` lines 29–35
- `GetTransactionsProcess::execute()` — `sync/src/relayer/get_transactions_process.rs` lines 33–39
- `GetBlockTransactionsProcess::execute()` — `sync/src/relayer/get_block_transactions_process.rs` lines 37–43

`TransactionsProcess::execute()` has no equivalent guard.

The `tx_filter` and `unknown_tx_hashes` mutexes are shared across all relay operations (`sync/src/types/mod.rs` lines 1018–1019). Holding them during a long iteration blocks:
1. Concurrent `TransactionsProcess` calls from other peers (same `tx_filter` lock).
2. `TransactionHashesProcess` — new tx-hash announcements cannot be processed.
3. The `ask_for_txs` timer — the node cannot issue `GetRelayTransactions` requests.
4. `send_bulk_of_tx_hashes` — outbound tx-hash broadcasts stall.

The rate limiter at `sync/src/relayer/mod.rs` lines 116–123 caps message frequency at 30 req/sec per (peer, message_type) but does not limit message size. The P2P decompressed-message size limit (~4 MB) allows approximately 40,000 minimal transactions per message, well above the 32,767 limit enforced by every other handler.

## Impact Explanation
**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker with one or more P2P connections can repeatedly send oversized `RelayTransactions` messages (up to 30/sec per connection per the rate limiter) to stall transaction propagation on targeted nodes. Targeting multiple nodes simultaneously degrades the entire network's ability to relay transactions, constituting network congestion achievable at minimal cost (a single established P2P connection and crafted molecule-encoded messages).

## Likelihood Explanation
Any connected peer without privilege can execute this attack. The attacker needs only to: (1) establish a P2P connection, (2) send `RelayTransactionHashes` to seed `unknown_tx_hashes`, and (3) respond to the node's `GetRelayTransactions` with a padded `RelayTransactions` message. No special knowledge, leaked keys, or victim mistakes are required. The attack is repeatable and can be sustained continuously within the rate limit.

## Recommendation
Add the same count guard used by every other relay handler at the top of `TransactionsProcess::execute()` in `sync/src/relayer/transactions_process.rs`:

```rust
pub fn execute(self) -> Status {
    let message_len = self.message.transactions().len();
    if message_len > MAX_RELAY_TXS_NUM_PER_BATCH {
        return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
            "Transactions count({message_len}) > MAX_RELAY_TXS_NUM_PER_BATCH({MAX_RELAY_TXS_NUM_PER_BATCH})"
        ));
    }
    // ... existing logic
}
```

This mirrors the pattern in `TransactionHashesProcess`, `GetTransactionsProcess`, and `GetBlockTransactionsProcess`.

## Proof of Concept
1. Establish a P2P connection to a CKB full node (RelayV3 protocol).
2. Send `RelayTransactionHashes` with 32,767 distinct transaction hashes. The node adds them to `unknown_tx_hashes` and replies with `GetRelayTransactions`.
3. Construct a `RelayTransactions` molecule message containing the 32,767 requested transactions plus N additional minimal transactions (empty inputs/outputs, ~100 bytes each), where N is chosen so the total decompressed size approaches the P2P message size limit (~4 MB), yielding ~40,000 total entries.
4. Send the crafted `RelayTransactions` message.
5. The victim node enters `TransactionsProcess::execute()`, acquires `tx_filter` and `unknown_tx_hashes` mutexes, and calls `to_entity().into_view()` on all ~40,000 entries while holding both locks.
6. Observe on the victim node: incoming `RelayTransactionHashes` from other peers are not processed (`TransactionHashesProcess` blocks on `tx_filter`), the `ask_for_txs` timer fires but cannot acquire `unknown_tx_hashes`, and transaction propagation latency spikes measurably.
7. Repeat at up to 30 messages/sec (rate limiter cap) to sustain the stall.