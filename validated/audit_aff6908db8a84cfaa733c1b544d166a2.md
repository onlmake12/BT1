Audit Report

## Title
Missing Count Limit on `RelayTransactions` Message Enables Lock-Contention DoS — (File: sync/src/relayer/transactions_process.rs)

## Summary
`TransactionsProcess::execute()` acquires two `parking_lot::Mutex` locks (`tx_filter` and `unknown_tx_hashes`) and iterates over every transaction in a peer-supplied `RelayTransactions` message with no prior count guard. Every other relay message handler enforces `MAX_RELAY_TXS_NUM_PER_BATCH` (32 767) before touching shared state. An unprivileged peer can send a single crafted `RelayTransactions` message containing the maximum number of transactions permitted by the P2P message-size limit, holding both mutexes for the entire iteration and stalling all concurrent relay operations that depend on those locks.

## Finding Description
`TransactionsProcess::execute()` immediately acquires both mutexes and begins iterating:

```rust
// sync/src/relayer/transactions_process.rs  lines 39-57
let txs: Vec<(TransactionView, Cycle)> = {
    let mut tx_filter = shared_state.tx_filter();        // parking_lot::Mutex held
    tx_filter.remove_expired();
    let unknown_tx_hashes = shared_state.unknown_tx_hashes(); // parking_lot::Mutex held

    self.message
        .transactions()
        .iter()
        .map(|tx| (tx.transaction().to_entity().into_view(), tx.cycles().into()))
        // hash computed for every entry while both locks are held
        .filter(|(tx, _)| { ... })
        .collect()
};
``` [1](#0-0) 

`to_entity().into_view()` performs hash computation and allocation for **every** entry in the message before the filter is applied, all while holding both locks. There is no count check anywhere in the function.

By contrast, every other handler checks the count first and returns `ProtocolMessageIsMalformed` before acquiring any lock:

| Handler | Guard location |
|---|---|
| `TransactionHashesProcess` | `transaction_hashes_process.rs` line 29 |
| `GetTransactionsProcess` | `get_transactions_process.rs` line 35 |
| `GetBlockTransactionsProcess` | `get_block_transactions_process.rs` line 37 |
| **`TransactionsProcess`** | **absent** | [2](#0-1) [3](#0-2) [4](#0-3) 

`MAX_RELAY_TXS_NUM_PER_BATCH` is 32 767: [5](#0-4) 

`TransactionsProcess::execute()` is called synchronously inside the async `try_process` dispatcher: [6](#0-5) 

The existing rate limiter (30 req/s per peer per message type) limits message frequency but does not bound the transaction count within a single message: [7](#0-6) 

`tx_filter` and `unknown_tx_hashes` are shared `parking_lot::Mutex` instances across all relay operations: [8](#0-7) 

## Impact Explanation
**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

While both mutexes are held, the following concurrent operations are blocked:
1. All other `TransactionsProcess` calls from other peers (contend on `tx_filter`).
2. `TransactionHashesProcess` calls — new tx-hash announcements cannot be processed (contend on `tx_filter`).
3. The `ask_for_txs` timer callback — the node cannot issue `GetRelayTransactions` requests (contends on `unknown_tx_hashes`).
4. `send_bulk_of_tx_hashes` — outbound tx-hash broadcasts stall (contends on `tx_filter`).

A single attacker with one P2P connection can repeatedly trigger this (rate-limited to 30/s, but each message can carry up to ~40 000 entries within the P2P size limit), causing sustained degradation of transaction propagation on the victim node. Targeting multiple nodes simultaneously degrades network-wide transaction relay with minimal cost.

## Likelihood Explanation
**Medium.** Any connected peer requires no privilege. The attack requires only one established P2P connection and the ability to craft a valid molecule-encoded `RelayTransactions` message. The attacker first sends `RelayTransactionHashes` to populate `unknown_tx_hashes` on the victim (ensuring the filter does not short-circuit early), then sends an oversized `RelayTransactions` response. The rate limiter does not prevent a single large message from holding the locks for an extended period.

## Recommendation
Add the same count guard used by every other relay handler at the top of `TransactionsProcess::execute()`, before any lock is acquired:

```rust
// sync/src/relayer/transactions_process.rs
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

This mirrors the pattern in `TransactionHashesProcess`, `GetTransactionsProcess`, and `GetBlockTransactionsProcess` and ensures the peer is banned for sending an oversized message.

## Proof of Concept
1. Establish a P2P connection to a CKB full node (RelayV3 protocol).
2. Send `RelayTransactionHashes` with up to 32 767 distinct transaction hashes. The node adds them to `unknown_tx_hashes` and replies with `GetRelayTransactions`.
3. Construct a `RelayTransactions` molecule message containing those 32 767 requested transactions plus additional minimal transactions (empty inputs/outputs, ~100 bytes each), padding the total decompressed size to approach the P2P message size limit (~4 MB → ~40 000 entries total).
4. Send the crafted message.
5. The victim node enters `TransactionsProcess::execute()`, acquires `tx_filter` and `unknown_tx_hashes`, and calls `to_entity().into_view()` (hash computation + allocation) on every entry while holding both locks.
6. During this window, observe on the victim:
   - Incoming `RelayTransactionHashes` from other peers are not processed (`TransactionHashesProcess` blocks on `tx_filter`).
   - The `ask_for_txs` timer fires but cannot acquire `unknown_tx_hashes`.
   - Transaction propagation latency spikes measurably.
7. Repeat at the rate-limiter ceiling (30 msg/s) to sustain the degradation.

### Citations

**File:** sync/src/relayer/transactions_process.rs (L39-57)
```rust
        let txs: Vec<(TransactionView, Cycle)> = {
            // ignore the tx if it's already known or it has never been requested before
            let mut tx_filter = shared_state.tx_filter();
            tx_filter.remove_expired();
            let unknown_tx_hashes = shared_state.unknown_tx_hashes();

            self.message
                .transactions()
                .iter()
                .map(|tx| (tx.transaction().to_entity().into_view(), tx.cycles().into()))
                .filter(|(tx, _)| {
                    !tx_filter.contains(&tx.hash())
                        && unknown_tx_hashes
                            .get_priority(&tx.hash())
                            .map(|priority| priority.requesting_peer() == Some(self.peer))
                            .unwrap_or_default()
                })
                .collect()
        };
```

**File:** sync/src/relayer/transaction_hashes_process.rs (L29-35)
```rust
            if relay_transaction_hashes.tx_hashes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "TxHashes count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
                    relay_transaction_hashes.tx_hashes().len(),
                    MAX_RELAY_TXS_NUM_PER_BATCH,
                ));
            }
```

**File:** sync/src/relayer/get_transactions_process.rs (L33-39)
```rust
        let message_len = self.message.tx_hashes().len();
        {
            if message_len > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "TxHashes count({message_len}) > MAX_RELAY_TXS_NUM_PER_BATCH({MAX_RELAY_TXS_NUM_PER_BATCH})",
                ));
            }
```

**File:** sync/src/relayer/get_block_transactions_process.rs (L37-43)
```rust
            if get_block_transactions.indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "Indexes count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
                    get_block_transactions.indexes().len(),
                    MAX_RELAY_TXS_NUM_PER_BATCH,
                ));
            }
```

**File:** sync/src/relayer/mod.rs (L60-61)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
pub const MAX_RELAY_TXS_BYTES_PER_BATCH: usize = 1024 * 1024;
```

**File:** sync/src/relayer/mod.rs (L116-123)
```rust
        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```

**File:** sync/src/relayer/mod.rs (L135-142)
```rust
            packed::RelayMessageUnionReader::RelayTransactions(reader) => {
                if reader.check_data() {
                    TransactionsProcess::new(reader, self, nc, peer).execute()
                } else {
                    StatusCode::ProtocolMessageIsMalformed
                        .with_context("RelayTransactions is invalid")
                }
            }
```

**File:** sync/src/types/mod.rs (L1018-1019)
```rust
            tx_filter: Mutex::new(TtlFilter::default()),
            unknown_tx_hashes: Mutex::new(KeyedPriorityQueue::new()),
```
