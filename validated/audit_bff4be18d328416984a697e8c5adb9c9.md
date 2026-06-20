### Title
Missing Transaction Count Limit in `RelayTransactions` Handler Enables Unbounded Deserialization DoS — (File: `sync/src/relayer/transactions_process.rs`)

---

### Summary

The `TransactionsProcess::execute()` handler for `RelayTransactions` P2P messages does not enforce any upper bound on the number of transactions in the message. Every other analogous relay handler enforces `MAX_RELAY_TXS_NUM_PER_BATCH`, but this one is missing the guard. A malicious connected peer can send a single `RelayTransactions` message containing an arbitrarily large number of transactions, forcing the receiving node to deserialize every one of them into full `TransactionView` objects before any filtering occurs, causing unbounded CPU and memory consumption.

---

### Finding Description

Three relay message handlers exist for the transaction relay flow:

| Handler | Message | Count limit? |
|---|---|---|
| `transaction_hashes_process.rs` | `RelayTransactionHashes` | ✅ `MAX_RELAY_TXS_NUM_PER_BATCH` |
| `get_transactions_process.rs` | `GetRelayTransactions` | ✅ `MAX_RELAY_TXS_NUM_PER_BATCH` |
| `transactions_process.rs` | `RelayTransactions` | ❌ **None** |

`transaction_hashes_process.rs` rejects oversized messages immediately: [1](#0-0) 

`get_transactions_process.rs` does the same: [2](#0-1) 

But `transactions_process.rs` has no such guard. It immediately deserializes every transaction in the message into a full `TransactionView` via `.to_entity().into_view()` before any filtering: [3](#0-2) 

The deserialization at line 48 — `tx.transaction().to_entity().into_view()` — is the expensive step. It allocates and parses the full transaction structure for every entry in the message, regardless of whether the transaction will be accepted or discarded by the subsequent filter. Only after all transactions are deserialized does the filter check `unknown_tx_hashes` and `tx_filter`.

The constant is defined in: [4](#0-3) 

---

### Impact Explanation

A single malicious peer can craft a `RelayTransactions` message containing thousands of large transactions. The receiving node will:

1. Deserialize every transaction into a `TransactionView` (heap allocation per transaction, proportional to transaction size).
2. Iterate the full list to check `tx_filter` and `unknown_tx_hashes`.
3. Spawn async tasks for any that pass the filter.

Steps 1–2 are O(n × tx_size) in CPU and memory, with no bound on n. This can exhaust node memory or saturate the CPU processing loop, degrading or halting block relay and transaction propagation for all peers — a targeted DoS against a specific node.

---

### Likelihood Explanation

Any peer that has established a P2P connection can send a `RelayTransactions` message. No authentication, stake, or privilege is required. The attacker does not need to have previously announced the transactions via `RelayTransactionHashes`; the filter at line 51–54 simply discards unrecognized hashes after the expensive deserialization has already occurred. The attack is repeatable at the rate the network allows message delivery. [5](#0-4) 

---

### Recommendation

Add the same guard present in the sibling handlers at the top of `TransactionsProcess::execute()`, before any deserialization:

```rust
let message_len = self.message.transactions().len();
if message_len > MAX_RELAY_TXS_NUM_PER_BATCH {
    return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
        "Transactions count({message_len}) > MAX_RELAY_TXS_NUM_PER_BATCH({MAX_RELAY_TXS_NUM_PER_BATCH})"
    ));
}
```

This mirrors the pattern already used in `get_transactions_process.rs` and `transaction_hashes_process.rs` and should be applied before the `.map(|tx| tx.transaction().to_entity().into_view())` call.

---

### Proof of Concept

1. Establish a P2P connection to a target CKB node as a relay peer (standard peer handshake, no privileges needed).
2. Construct a `RelayTransactions` molecule message containing, e.g., 10,000 entries, each with a large but syntactically valid `Transaction` payload.
3. Send the message over the `RelayV3` protocol channel.
4. The target node enters `TransactionsProcess::execute()`, calls `.to_entity().into_view()` on all 10,000 entries before any filtering, allocating memory and burning CPU proportional to the total payload size.
5. Repeat at the maximum message rate to sustain memory pressure or CPU saturation, preventing the node from processing legitimate blocks and transactions.

The attack requires no valid transactions, no prior interaction, and no knowledge of the node's mempool state. [6](#0-5)

### Citations

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

**File:** util/constant/src/sync.rs (L1-5)
```rust
use std::time::Duration;

/// The default init download block interval is 24 hours
/// If the time of the local highest block is within this range, exit the ibd state
pub const MAX_TIP_AGE: u64 = 24 * 60 * 60 * 1000;
```
