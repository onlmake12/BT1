### Title
`GetBlockTransactionsProcess` Allows Duplicate `indexes` Without Deduplication, Enabling Response Amplification - (File: `sync/src/relayer/get_block_transactions_process.rs`)

### Summary
`GetBlockTransactionsProcess::execute()` enforces a count limit on the `indexes` field of a `GetBlockTransactions` P2P message but performs **no duplicate check**. An unprivileged peer can send up to `MAX_RELAY_TXS_NUM_PER_BATCH` (32,767) copies of the same transaction index, causing the node to allocate memory for and transmit a `BlockTransactions` response containing 32,767 copies of the same transaction — a significant CPU, memory, and bandwidth amplification.

### Finding Description
In `GetBlockTransactionsProcess::execute()`, the handler checks that `indexes.len() <= MAX_RELAY_TXS_NUM_PER_BATCH` but never deduplicates the list before fetching transactions from the block store:

```rust
// sync/src/relayer/get_block_transactions_process.rs L37-43
if get_block_transactions.indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
    return StatusCode::ProtocolMessageIsMalformed.with_context(...);
}
``` [1](#0-0) 

Immediately after, the handler iterates over every index — including duplicates — and fetches the corresponding transaction from the block:

```rust
// sync/src/relayer/get_block_transactions_process.rs L61-71
let transactions = self
    .message
    .indexes()
    .iter()
    .filter_map(|i| {
        block
            .transactions()
            .get(Into::<u32>::into(i) as usize)
            .cloned()
    })
    .collect::<Vec<_>>();
``` [2](#0-1) 

The resulting `transactions` vector — potentially containing 32,767 copies of the same transaction — is then serialized into a `BlockTransactions` message and sent back to the peer with **no byte-size limit** on the response: [3](#0-2) 

Compare this with `GetBlockProposalProcess` and `GetTransactionsProcess`, which both enforce `MAX_RELAY_TXS_BYTES_PER_BATCH` (1 MiB) on their responses. `GetBlockTransactionsProcess` has no such guard. [4](#0-3) 

The `uncle_indexes` field has the same problem: it is bounded by `max_uncles_num` but not deduplicated before fetching uncles. [5](#0-4) 

### Impact Explanation
An attacker sends a single `GetBlockTransactions` message with 32,767 copies of index `0` (the cellbase or any large committed transaction). The victim node:
1. Allocates a `Vec` of 32,767 cloned `TransactionView` objects in memory.
2. Serializes them into a single `BlockTransactions` molecule message (potentially tens of megabytes).
3. Attempts to transmit this over the P2P connection.

Even if the network layer drops an oversized frame, the node has already paid the CPU and memory cost of building it. Repeated requests from multiple peers (or a single peer cycling through connections) can exhaust node memory or saturate outbound bandwidth, degrading service for legitimate peers.

### Likelihood Explanation
Any unauthenticated P2P peer that has completed the handshake can send `RelayMessage::GetBlockTransactions`. The message is small (~128 KB for 32,767 `u32` indexes) and requires no prior knowledge beyond a valid block hash (obtainable from public chain data). The attack is trivially scriptable and repeatable. [6](#0-5) 

### Recommendation
Add a duplicate-index check immediately after the length check, mirroring the pattern used in `GetBlockProposalProcess` and `GetTransactionsProcess`:

```rust
// After the length check:
let indexes_set: HashSet<u32> = self.message.indexes().iter()
    .map(Into::<u32>::into).collect();
if indexes_set.len() != self.message.indexes().len() {
    return StatusCode::RequestDuplicate.with_context("Duplicate indexes in GetBlockTransactions");
}
// Same for uncle_indexes
```

Additionally, apply a `MAX_RELAY_TXS_BYTES_PER_BATCH` byte-size cap on the outgoing `BlockTransactions` response, consistent with how `GetBlockProposalProcess` limits its `BlockProposal` response. [4](#0-3) 

### Proof of Concept
1. Connect to a CKB node as a P2P peer using the `RelayV3` protocol.
2. Obtain any valid block hash `H` from the chain that contains at least one committed transaction.
3. Craft a `GetBlockTransactions` message:
   ```
   block_hash: H
   indexes: [0, 0, 0, ..., 0]   // 32,767 copies of index 0
   uncle_indexes: []
   ```
4. Send the message. The node will respond with a `BlockTransactions` message containing 32,767 copies of the cellbase transaction, consuming proportional memory and CPU on the victim node and saturating the outbound connection. [7](#0-6)

### Citations

**File:** sync/src/relayer/get_block_transactions_process.rs (L33-101)
```rust
    pub async fn execute(self) -> Status {
        let shared = self.relayer.shared();
        {
            let get_block_transactions = self.message;
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
        }

        let block_hash = self.message.block_hash().to_entity();
        debug_target!(
            crate::LOG_TARGET_RELAY,
            "get_block_transactions {}",
            block_hash
        );

        if let Some(block) = shared.store().get_block(&block_hash) {
            let transactions = self
                .message
                .indexes()
                .iter()
                .filter_map(|i| {
                    block
                        .transactions()
                        .get(Into::<u32>::into(i) as usize)
                        .cloned()
                })
                .collect::<Vec<_>>();

            let uncles = self
                .message
                .uncle_indexes()
                .iter()
                .filter_map(|i| block.uncles().get(Into::<u32>::into(i) as usize))
                .collect::<Vec<_>>();

            let content = packed::BlockTransactions::new_builder()
                .block_hash(block_hash)
                .transactions(
                    transactions
                        .into_iter()
                        .map(|tx| tx.data())
                        .collect::<Vec<_>>(),
                )
                .uncles(
                    uncles
                        .into_iter()
                        .map(|uncle| uncle.data())
                        .collect::<Vec<_>>(),
                )
                .build();
            let message = packed::RelayMessage::new_builder().set(content).build();

            return async_send_message_to(&self.nc, self.peer, &message).await;
        }

        Status::ok()
    }
```

**File:** sync/src/relayer/get_block_proposal_process.rs (L79-95)
```rust
        let mut relay_bytes = 0;
        let mut relay_proposals = Vec::new();
        for (_, tx) in fetched_transactions {
            let data = tx.data();
            let tx_size = data.total_size();
            if relay_bytes + tx_size > MAX_RELAY_TXS_BYTES_PER_BATCH {
                self.send_block_proposals(std::mem::take(&mut relay_proposals))
                    .await;
                relay_bytes = tx_size;
            } else {
                relay_bytes += tx_size;
            }
            relay_proposals.push(data);
        }
        if !relay_proposals.is_empty() {
            attempt!(self.send_block_proposals(relay_proposals).await);
        }
```

**File:** sync/src/relayer/mod.rs (L146-155)
```rust
            packed::RelayMessageUnionReader::GetRelayTransactions(reader) => {
                GetTransactionsProcess::new(reader, self, nc, peer)
                    .execute()
                    .await
            }
            packed::RelayMessageUnionReader::GetBlockTransactions(reader) => {
                GetBlockTransactionsProcess::new(reader, self, nc, peer)
                    .execute()
                    .await
            }
```
