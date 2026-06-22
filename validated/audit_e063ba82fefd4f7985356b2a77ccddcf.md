### Title
Missing Duplicate Index Validation in `GetBlockTransactionsProcess` Enables Bandwidth Amplification — (File: `sync/src/relayer/get_block_transactions_process.rs`)

---

### Summary

The `GetBlockTransactionsProcess::execute()` handler processes incoming `GetBlockTransactions` relay messages from peers. It validates that the number of requested `indexes` does not exceed `MAX_RELAY_TXS_NUM_PER_BATCH`, but performs **no duplicate check** on those indexes. An unprivileged connected peer can send a `GetBlockTransactions` message containing 256 copies of the same transaction index (e.g., index `0`, the cellbase), causing the victim node to look up and serialize the same transaction 256 times and transmit all copies back in a `BlockTransactions` response — a direct bandwidth and CPU amplification attack.

---

### Finding Description

In `sync/src/relayer/get_block_transactions_process.rs`, `GetBlockTransactionsProcess::execute()` performs two bounds checks on the incoming message:

```rust
if get_block_transactions.indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH { … }
if get_block_transactions.uncle_indexes().len() > shared