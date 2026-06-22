### Title
Missing `MAX_RELAY_TXS_NUM_PER_BATCH` Count Check in `TransactionsProcess` Allows Oversized Relay Batch — (`sync/src/relayer/transactions_process.rs`)

---

### Summary

The CKB relay protocol enforces a `MAX_RELAY_TXS_NUM_PER_BATCH` count limit in both `TransactionHashesProcess` (announcement) and `GetTransactionsProcess` (request), but the analogous limit is entirely absent in `TransactionsProcess` (delivery). An unprivileged peer can send a `RelayTransactions` message containing far more transactions than the protocol intends to allow in a single batch, bypassing the guard that exists on every other relay message type.

---

### Finding Description

The relay protocol defines three message types that form a pipeline:

1. **`RelayTransactionHashes`** → handled by `TransactionHashesProcess`
2. **`GetRelayTransactions`** → handled by `GetTransactionsProcess`
3. **`RelayTransactions`** → handled by `TransactionsProcess`

Both step 1 and step 2 enforce the batch count limit:

`TransactionHashesProcess::execute()` in `sync/src/relayer/transaction_hashes_process.rs` lines 29–35:
```rust
if relay_transaction_hashes.tx_hashes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
    return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
        "TxHashes count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
        ...
    ));
}
```

`GetTransactionsProcess::execute()` in `sync/src/relayer/get_transactions_process.rs` lines 35–39:
```rust
if message_len > MAX_RELAY_TXS_NUM_PER_BATCH {
    return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
        "TxHashes count({message_len}) > MAX_RELAY_TXS_NUM_PER_BATCH({MAX_RELAY_TXS_NUM_PER_BATCH})",
    ));
}
```

But `TransactionsProcess::execute()` in `sync/src/relayer/transactions_process.rs` lines 37–96 contains **no such count check**. It immediately iterates over all transactions in the message:
```rust
pub fn execute(self) -> Status {
    let shared_state = self.relayer.shared().state();
    let txs: Vec<(TransactionView, Cycle)> = {
        ...
        self.message
            .transactions()
            .iter()
            .map(|tx| (tx.transaction().to_entity().into_view(), tx.cycles().into()))
            .filter(...)
            .collect()
    };
    ...
```

There is no guard on `self.message.transactions().len()` before the iteration and deserialization begin.

---

### Impact Explanation

An attacker who has previously caused the local node to request transactions (by sending multiple `RelayTransactionHashes` messages, each within the per-message limit) can then deliver all of those transactions in a single `RelayTransactions` message that far exceeds `MAX_RELAY_TXS_NUM_PER_BATCH`. The node will:

1. Deserialize and iterate the entire oversized vector.
2. Allocate a `Vec<(TransactionView, Cycle)>` proportional to the number of transactions.
3. Spawn an async task that calls `tx_pool.submit_remote_tx` for each entry.

This constitutes a resource-exhaustion (memory + CPU) vector reachable from any unprivileged relay peer, with no authentication required.

---

### Likelihood Explanation

The attack requires only that the attacker be a connected relay peer. The attacker controls which transaction hashes they announce, so they can trivially pre-stage the node to request many transactions from them across multiple `RelayTransactionHashes` messages (each individually within the limit), then deliver all of them in one oversized `RelayTransactions` message. No special privilege, key, or majority hash power is needed.

---

### Recommendation

Add the same count guard at the top of `TransactionsProcess::execute()` that already exists in `TransactionHashesProcess` and `GetTransactionsProcess`:

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

---

### Proof of Concept

**Attack path:**

1. Connect to a CKB node as a relay peer.
2. Send `N` separate `RelayTransactionHashes` messages, each containing `MAX_RELAY_TXS_NUM_PER_BATCH` distinct (fabricated) tx hashes. The node records these as `unknown_tx_hashes` and issues `GetRelayTransactions` requests back to the attacker.
3. Respond with a single `RelayTransactions` message containing all `N × MAX_RELAY_TXS_NUM_PER_BATCH` transactions.
4. `TransactionsProcess::execute()` has no count guard; it deserializes and iterates the entire vector, allocating memory and spawning async submission tasks for each entry, exhausting node resources.

**Root cause — missing check (compare the three handlers):**

`transaction_hashes_process.rs` lines 29–35 — check present: [1](#0-0) 

`get_transactions_process.rs` lines 35–39 — check present: [2](#0-1) 

`transactions_process.rs` lines 37–57 — check **absent**, iteration begins immediately: [3](#0-2)

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

**File:** sync/src/relayer/get_transactions_process.rs (L35-39)
```rust
            if message_len > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "TxHashes count({message_len}) > MAX_RELAY_TXS_NUM_PER_BATCH({MAX_RELAY_TXS_NUM_PER_BATCH})",
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
