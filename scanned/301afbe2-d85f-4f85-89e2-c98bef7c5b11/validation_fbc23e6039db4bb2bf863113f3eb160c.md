### Title
Duplicate-Index Amplification in `GetBlockTransactionsProcess::execute` Enables Memory Exhaustion and Bandwidth Saturation — (`sync/src/relayer/get_block_transactions_process.rs`)

### Summary

An unprivileged remote peer can send a `GetBlockTransactions` message with up to 32,767 copies of the same valid transaction index. The handler performs no deduplication and applies no byte-size cap before serializing and sending the response, allowing a single small request to force the node to allocate and transmit an arbitrarily large `BlockTransactions` message.

### Finding Description

`GetBlockTransactionsProcess::execute` enforces only a count check against `MAX_RELAY_TXS_NUM_PER_BATCH` (32,767): [1](#0-0) 

The check is a strict `>`, so exactly 32,767 indexes are accepted. After that, the handler maps each index directly to a transaction clone with no deduplication: [2](#0-1) 

All collected transactions — including 32,767 copies of the same one — are packed into a single `BlockTransactions` message and sent with no byte cap: [3](#0-2) 

**Contrast with sibling handlers that correctly guard against this:**

`GetTransactionsProcess` deduplicates via a `HashSet` and rejects duplicates: [4](#0-3) 

It also enforces `MAX_RELAY_TXS_BYTES_PER_BATCH` before sending: [5](#0-4) 

`GetBlockProposalProcess` applies the same two guards (dedup + byte cap): [6](#0-5) [7](#0-6) 

`GetBlockTransactionsProcess` imports only `MAX_RELAY_TXS_NUM_PER_BATCH` — `MAX_RELAY_TXS_BYTES_PER_BATCH` is never imported or applied: [8](#0-7) 

### Impact Explanation

`MAX_RELAY_TXS_NUM_PER_BATCH` is 32,767: [9](#0-8) 

A single CKB transaction can be hundreds of KB. With 32,767 duplicate index entries pointing to one 500 KB transaction, the node must allocate and serialize ~16 GB into a single response. Even a modest 10 KB transaction yields ~320 MB per request. Any number of peers can issue this simultaneously, causing:

- **Memory exhaustion** during collection and serialization of the duplicated transaction vector.
- **Outbound bandwidth saturation** from the oversized response.
- **Node crash or severe degradation** with no PoW or authentication barrier.

### Likelihood Explanation

The attacker needs only a standard P2P connection and knowledge of any block hash present in the target node's store — both trivially obtained from the public chain. No privilege, key, or hashpower is required. The attack is repeatable and parallelizable.

### Recommendation

Apply both fixes that the sibling handlers already use:

1. **Deduplicate indexes** before lookup — collect into a `HashSet<u32>` and return `StatusCode::RequestDuplicate` if `set.len() != indexes.len()`.
2. **Enforce `MAX_RELAY_TXS_BYTES_PER_BATCH`** — accumulate serialized size and split or truncate the response when the cap is exceeded, mirroring `GetTransactionsProcess` lines 87–101.

### Proof of Concept

```rust
// Unit test sketch
let block = build_block_with_one_tx(500_000); // 500 KB transaction
store.insert_block(&block);

let indexes: Vec<u32> = vec![0u32; 32767];
let msg = build_get_block_transactions(block.hash(), indexes);
let response = execute_handler(msg).await;

// Response contains 32767 × 500 KB ≈ 16 GB of serialized data
assert!(response.serialized_size() > MAX_RELAY_TXS_BYTES_PER_BATCH);
```

### Citations

**File:** sync/src/relayer/get_block_transactions_process.rs (L1-1)
```rust
use crate::relayer::{MAX_RELAY_TXS_NUM_PER_BATCH, Relayer};
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

**File:** sync/src/relayer/get_block_transactions_process.rs (L61-71)
```rust
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
```

**File:** sync/src/relayer/get_block_transactions_process.rs (L80-97)
```rust
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
```

**File:** sync/src/relayer/get_transactions_process.rs (L54-61)
```rust
            let tx_hashes_set: HashSet<_> = tx_hashes
                .iter()
                .map(|tx_hash| packed::ProposalShortId::from_tx_hash(&tx_hash.to_entity()))
                .collect();

            if message_len != tx_hashes_set.len() {
                return StatusCode::RequestDuplicate.with_context("Request duplicate transaction");
            }
```

**File:** sync/src/relayer/get_transactions_process.rs (L87-101)
```rust
            let mut relay_bytes = 0;
            let mut relay_txs = Vec::new();
            for tx in transactions {
                if relay_bytes + tx.total_size() > MAX_RELAY_TXS_BYTES_PER_BATCH {
                    self.send_relay_transactions(std::mem::take(&mut relay_txs))
                        .await;
                    relay_bytes = tx.total_size();
                } else {
                    relay_bytes += tx.total_size();
                }
                relay_txs.push(tx);
            }
            if !relay_txs.is_empty() {
                attempt!(self.send_relay_transactions(relay_txs).await);
            }
```

**File:** sync/src/relayer/get_block_proposal_process.rs (L47-52)
```rust
        let proposals: HashSet<packed::ProposalShortId> =
            self.message.proposals().to_entity().into_iter().collect();

        if proposals.len() != message_len {
            return StatusCode::RequestDuplicate.with_context("Request duplicate proposal");
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

**File:** util/constant/src/sync.rs (L68-68)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
```
