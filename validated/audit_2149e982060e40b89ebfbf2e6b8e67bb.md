Audit Report

## Title
Missing `MAX_RELAY_TXS_NUM_PER_BATCH` Count Check in `TransactionsProcess::execute()` Allows Oversized Relay Batch Deserialization — (`sync/src/relayer/transactions_process.rs`)

## Summary
`TransactionsProcess::execute()` performs no count check on the incoming `RelayTransactions` message before iterating and deserializing all transactions it contains. Both sibling handlers — `TransactionHashesProcess` and `GetTransactionsProcess` — enforce `MAX_RELAY_TXS_NUM_PER_BATCH` (32,767) at their entry points, but `TransactionsProcess` omits this guard entirely. An unprivileged relay peer can send a single `RelayTransactions` message containing an arbitrarily large number of transactions, forcing the node to deserialize every entry before the `unknown_tx_hashes` filter is applied, constituting a CPU and memory exhaustion vector.

## Finding Description

**Confirmed missing check — `transactions_process.rs` lines 37–57:**

`TransactionsProcess::execute()` immediately enters the iterator chain with no count guard:

```rust
pub fn execute(self) -> Status {
    let shared_state = self.relayer.shared().state();
    let txs: Vec<(TransactionView, Cycle)> = {
        let mut tx_filter = shared_state.tx_filter();
        tx_filter.remove_expired();
        let unknown_tx_hashes = shared_state.unknown_tx_hashes();

        self.message
            .transactions()
            .iter()
            .map(|tx| (tx.transaction().to_entity().into_view(), tx.cycles().into()))
            .filter(|(tx, _)| { ... })
            .collect()
    };
``` [1](#0-0) 

The critical detail is the iterator ordering: `.map()` (which calls `.to_entity().into_view()`, performing full deserialization of each transaction) is applied to **every element** before `.filter()` is evaluated. In Rust's lazy iterator model, for each element the map closure runs first, then the filter closure. This means all `N` transactions in the message are fully deserialized regardless of how many pass the filter.

**Confirmed present in `transaction_hashes_process.rs` lines 29–35:** [2](#0-1) 

**Confirmed present in `get_transactions_process.rs` lines 35–39:** [3](#0-2) 

**`MAX_RELAY_TXS_NUM_PER_BATCH` = 32,767** (confirmed in `sync/src/relayer/mod.rs` line 60 and `util/constant/src/sync.rs` line 68): [4](#0-3) 

**No network-level byte-size check on incoming `RelayTransactions`:** The `received()` handler in `mod.rs` dispatches directly to `TransactionsProcess::new(...).execute()` with no prior size validation: [5](#0-4) 

**The `unknown_tx_hashes` filter does not mitigate deserialization cost.** The filter only limits which transactions are collected into `txs` and subsequently submitted to the pool — it does not prevent deserialization of all entries in the message. The soft per-peer cap `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` (= 32,767) and global cap `MAX_UNKNOWN_TX_HASHES_SIZE` (= 50,000) bound what passes the filter, not what is deserialized: [6](#0-5) 

## Impact Explanation

An attacker can send a `RelayTransactions` message containing far more than 32,767 transactions. The node will deserialize every transaction in the message (CPU + heap allocation per `TransactionView`) before the filter discards the excess. With no byte-size or count guard at the application layer, the attacker can drive unbounded memory allocation and CPU consumption on the receiving node, potentially crashing it.

**Applicable impact class: High (10,001–15,000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

The attack requires only a standard relay peer connection — no authentication, no hash power, no key material. The attacker fully controls the content of the `RelayTransactions` message. The `ask_for_txs` path in `mod.rs` (line 614) truncates outgoing `GetRelayTransactions` to `MAX_RELAY_TXS_NUM_PER_BATCH`, but nothing prevents the attacker from responding with a message containing far more transactions than were requested. The attack is repeatable at will from any connected peer. [7](#0-6) 

## Recommendation

Add the same count guard at the top of `TransactionsProcess::execute()` that exists in the two sibling handlers:

```rust
pub fn execute(self) -> Status {
+   let tx_count = self.message.transactions().len();
+   if tx_count > MAX_RELAY_TXS_NUM_PER_BATCH {
+       return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
+           "RelayTransactions count({tx_count}) > MAX_RELAY_TXS_NUM_PER_BATCH({MAX_RELAY_TXS_NUM_PER_BATCH})",
+       ));
+   }
    let shared_state = self.relayer.shared().state();
    ...
```

`MAX_RELAY_TXS_NUM_PER_BATCH` must be imported from `crate::relayer` (as already done in the sibling files). Returning `StatusCode::ProtocolMessageIsMalformed` will cause the peer to be banned via the existing ban logic in `process()`. [8](#0-7) 

## Proof of Concept

**Manual steps:**

1. Connect to a CKB node as a relay peer.
2. Send one or more `RelayTransactionHashes` messages (each ≤ 32,767 hashes) containing fabricated tx hashes. The node records these in `unknown_tx_hashes` and issues `GetRelayTransactions` back to the attacker.
3. Construct a single `RelayTransactions` message containing `N` transactions where `N >> MAX_RELAY_TXS_NUM_PER_BATCH` (e.g., 500,000). For the transactions that match the requested hashes, include valid-looking entries; pad the rest with arbitrary data.
4. Send the oversized message to the node.
5. `TransactionsProcess::execute()` enters the `.map().filter().collect()` chain with no count guard. All `N` transactions are deserialized via `.to_entity().into_view()` before any filtering occurs, consuming CPU and heap proportional to `N`.

**Unit test plan:**

Construct a `packed::RelayTransactions` with `MAX_RELAY_TXS_NUM_PER_BATCH + 1` entries and call `TransactionsProcess::new(...).execute()`. Assert the return value is `StatusCode::ProtocolMessageIsMalformed` (currently it is not — the test will fail, confirming the bug). After applying the fix, the same test should pass. [9](#0-8)

### Citations

**File:** sync/src/relayer/transactions_process.rs (L37-57)
```rust
    pub fn execute(self) -> Status {
        let shared_state = self.relayer.shared().state();
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

**File:** sync/src/relayer/transaction_hashes_process.rs (L1-1)
```rust
use crate::relayer::{MAX_RELAY_TXS_NUM_PER_BATCH, Relayer};
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

**File:** sync/src/relayer/get_transactions_process.rs (L35-39)
```rust
            if message_len > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "TxHashes count({message_len}) > MAX_RELAY_TXS_NUM_PER_BATCH({MAX_RELAY_TXS_NUM_PER_BATCH})",
                ));
            }
```

**File:** sync/src/relayer/mod.rs (L60-61)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
pub const MAX_RELAY_TXS_BYTES_PER_BATCH: usize = 1024 * 1024;
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

**File:** sync/src/relayer/mod.rs (L614-614)
```rust
                tx_hashes.truncate(MAX_RELAY_TXS_NUM_PER_BATCH);
```

**File:** util/constant/src/sync.rs (L69-72)
```rust
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```
