### Title
Missing Count Sanity Check on `RelayTransactions` Message Allows Unbounded Deserialization Work — (`sync/src/relayer/transactions_process.rs`)

### Summary

The `TransactionsProcess` handler for the `RelayTransactions` P2P relay message does not validate the number of transactions in the message before iterating and deserializing them. Every other relay message handler in the same dispatch loop enforces `MAX_RELAY_TXS_NUM_PER_BATCH` (32,767) as an upper bound before processing. The missing check allows any connected peer to force the receiving node to deserialize an arbitrarily large number of transactions per message, bounded only by the P2P frame size.

### Finding Description

In `sync/src/relayer/mod.rs`, the relay message dispatcher routes `RelayTransactions` to `TransactionsProcess` after calling `check_data()` for molecule encoding validation: [1](#0-0) 

`TransactionsProcess::execute()` then immediately iterates over all transactions in the message, calling `.to_entity().into_view()` (full deserialization) on each one before any filtering occurs: [2](#0-1) 

The `.map()` (deserialization) runs before `.filter()`, so every transaction in the message is fully deserialized regardless of whether it passes the filter. There is no count check anywhere in this path.

Contrast this with every sibling handler, all of which enforce `MAX_RELAY_TXS_NUM_PER_BATCH` before doing any work:

- `TransactionHashesProcess` checks `tx_hashes().len() > MAX_RELAY_TXS_NUM_PER_BATCH`: [3](#0-2) 

- `GetTransactionsProcess` checks `message_len > MAX_RELAY_TXS_NUM_PER_BATCH`: [4](#0-3) 

- `GetBlockTransactionsProcess` checks both `indexes().len()` and `uncle_indexes().len()`: [5](#0-4) 

- `GetBlockProposalProcess` checks proposals count against a consensus-derived limit: [6](#0-5) 

- `BlockProposalProcess` checks transactions count against a consensus-derived limit: [7](#0-6) 

`TransactionsProcess` is the only handler in this dispatch loop with no such guard.

The constants involved: [8](#0-7) 

### Impact Explanation

A malicious connected peer sends a `RelayTransactions` message containing the maximum number of transactions that fit within the P2P frame size. The `check_data()` call performs O(n) molecule validation, and then `execute()` performs O(n) full transaction deserialization (including hash computation via `into_view()`) before the filter discards most or all of them. With the rate limiter set to 30 requests per second per peer per message type, a single peer can force ~30 × (frame_size / min_tx_size) deserialization operations per second. With multiple peers this scales linearly. The result is sustained CPU and memory pressure on the receiving node, degrading its ability to process legitimate blocks and transactions.

**Impact: Medium** — sustained resource exhaustion reachable by any connected peer; does not directly cause consensus failure but degrades node availability.

### Likelihood Explanation

Any peer that completes the P2P handshake can send `RelayTransactions` messages. No privileged key, majority hashpower, or social engineering is required. The attack is trivially scriptable: construct a molecule-valid `RelayTransactions` message with the maximum number of minimal-size transactions and send it at the rate-limiter ceiling.

**Likelihood: Medium** — requires only a connected peer; rate limiter provides partial mitigation but does not eliminate the attack surface.

### Recommendation

Add a count check at the top of `TransactionsProcess::execute()`, consistent with all sibling handlers:

```rust
pub fn execute(self) -> Status {
    let tx_count = self.message.transactions().len();
    if tx_count > MAX_RELAY_TXS_NUM_PER_BATCH {
        return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
            "Transactions count({tx_count}) > MAX_RELAY_TXS_NUM_PER_BATCH({MAX_RELAY_TXS_NUM_PER_BATCH})"
        ));
    }
    // ... existing logic
}
```

### Proof of Concept

1. Connect to a CKB node as a peer (complete the P2P handshake).
2. Construct a molecule-valid `RelayTransactions` message containing `MAX_RELAY_TXS_NUM_PER_BATCH` (32,767) minimal `RelayTransaction` entries (each ~88 bytes, totalling ~2.9 MB).
3. Send this message at 30 messages/second (the rate-limiter ceiling for this message type).
4. The target node will call `check_data()` (O(n) molecule validation) and then `TransactionsProcess::execute()` (O(n) full deserialization via `to_entity().into_view()`) for every message, with no count guard to short-circuit. Observe elevated CPU usage on the target node proportional to the number of transactions per message.

### Citations

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

**File:** sync/src/relayer/get_transactions_process.rs (L33-40)
```rust
        let message_len = self.message.tx_hashes().len();
        {
            if message_len > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "TxHashes count({message_len}) > MAX_RELAY_TXS_NUM_PER_BATCH({MAX_RELAY_TXS_NUM_PER_BATCH})",
                ));
            }
        }
```

**File:** sync/src/relayer/get_block_transactions_process.rs (L37-50)
```rust
            if get_block_transactions.indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "Indexes count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
                    get_block_transactions.indexes().len(),
                    MAX_RELAY_TXS_NUM_PER_BATCH,
                ));
            }
            if get_block_transactions.uncle_indexes().len() > shared.consensus().max_uncles_num() {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "UncleIndexes count({}) > consensus max_uncles_num({})",
                    get_block_transactions.uncle_indexes().len(),
                    shared.consensus().max_uncles_num(),
                ));
            }
```

**File:** sync/src/relayer/get_block_proposal_process.rs (L34-44)
```rust
        let message_len = self.message.proposals().len();
        {
            // The block proposal request is separate from uncles,
            // so here the limit is only used to calculate the maximum value of uncles
            let limit = shared.consensus().max_block_proposals_limit()
                * (shared.consensus().max_uncles_num() as u64);
            if message_len as u64 > limit {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "GetBlockProposal proposals count({message_len}) > consensus max_block_proposals_limit({limit})"
                ));
            }
```

**File:** sync/src/relayer/block_proposal_process.rs (L26-36)
```rust
            let block_proposals = self.message;
            let limit = shared.consensus().max_block_proposals_limit()
                * (shared.consensus().max_uncles_num() as u64);
            if (block_proposals.transactions().len() as u64) > limit {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "Transactions count({}) > consensus max_block_proposals_limit({}) * max_uncles_num({})",
                    block_proposals.transactions().len(),
                    shared.consensus().max_block_proposals_limit(),
                    shared.consensus().max_uncles_num(),
                ));
            }
```
