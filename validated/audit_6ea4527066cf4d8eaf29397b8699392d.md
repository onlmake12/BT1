### Title
Unbounded Response Amplification via Duplicate Indexes in `GetBlockTransactionsProcess::execute` — (`sync/src/relayer/get_block_transactions_process.rs`)

### Summary

`GetBlockTransactionsProcess::execute` accepts up to `MAX_RELAY_TXS_NUM_PER_BATCH` (32,767) transaction indexes from a remote peer, but performs **no deduplication** and applies **no byte-size cap** on the resulting `BlockTransactions` response. An unprivileged peer can send a single `GetBlockTransactions` message with 32,767 identical valid indexes pointing to a large stored transaction, causing the victim node to allocate, serialize, and transmit a response that is orders of magnitude larger than the request.

### Finding Description

The guard in `execute` only checks the *count* of indexes: [1](#0-0) 

After passing that check, the code iterates over every index — including duplicates — and clones the corresponding transaction for each occurrence: [2](#0-1) 

The resulting `Vec<Transaction>` is packed directly into a `BlockTransactions` message and sent with no size check: [3](#0-2) 

`MAX_RELAY_TXS_NUM_PER_BATCH` is 32,767: [4](#0-3) 

`MAX_RELAY_TXS_BYTES_PER_BATCH` (1 MiB) is defined but **never used** in this code path: [5](#0-4) 

### Contrast with `GetBlockProposalProcess`

`GetBlockProposalProcess` has **both** missing guards:

1. **Duplicate rejection** — it compares `proposals.len()` against `message_len` and returns `RequestDuplicate` if they differ: [6](#0-5) 

2. **Byte-size cap** — it batches responses and flushes when `relay_bytes + tx_size > MAX_RELAY_TXS_BYTES_PER_BATCH`: [7](#0-6) 

Neither protection exists in `GetBlockTransactionsProcess`.

### Impact Explanation

- **Memory exhaustion on the victim**: building the response `Vec` for 32,767 copies of a large transaction allocates that memory in the relay thread before any send occurs.
- **Outbound bandwidth exhaustion**: the serialized `BlockTransactions` message is transmitted in full.
- **Relay-thread saturation**: the relay async task is occupied for the duration of the allocation and send.
- A single malicious peer can repeat this at low cost (one small P2P message per amplification event).

### Likelihood Explanation

The attacker only needs to know the hash of any block stored by the victim (trivially obtained from the chain) and the index of any large transaction within it. No authentication, PoW, or privileged access is required. The `GetBlockTransactions` message is a standard relay protocol message accepted from any connected peer.

### Recommendation

Apply both fixes present in `GetBlockProposalProcess` to `GetBlockTransactionsProcess::execute`:

1. **Deduplicate indexes** before processing (e.g., collect into a `HashSet<u32>` and reject if `deduped.len() != original_len`).
2. **Enforce a byte-size cap**: accumulate the serialized size of transactions to be included in the response and stop (or batch) once `MAX_RELAY_TXS_BYTES_PER_BATCH` is reached.

### Proof of Concept

```
1. Attacker connects to victim as a normal P2P peer.
2. Attacker observes any block hash H on-chain that contains a large transaction at index 0.
3. Attacker sends:
     GetBlockTransactions {
       block_hash: H,
       indexes: [0, 0, 0, ..., 0],   // 32,767 entries, all identical
       uncle_indexes: []
     }
4. Victim's execute():
   - Passes the count check (32767 <= 32767).
   - Loads the block from store.
   - filter_map produces 32,767 clones of the same transaction.
   - Builds and sends a BlockTransactions message containing 32,767 copies.
5. If the transaction is T bytes, the response is 32,767 × T bytes,
   allocated and serialized from a single ~200-byte request.
```

### Citations

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

**File:** util/constant/src/sync.rs (L68-68)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
```

**File:** sync/src/relayer/mod.rs (L60-61)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
pub const MAX_RELAY_TXS_BYTES_PER_BATCH: usize = 1024 * 1024;
```

**File:** sync/src/relayer/get_block_proposal_process.rs (L50-52)
```rust
        if proposals.len() != message_len {
            return StatusCode::RequestDuplicate.with_context("Request duplicate proposal");
        }
```

**File:** sync/src/relayer/get_block_proposal_process.rs (L84-90)
```rust
            if relay_bytes + tx_size > MAX_RELAY_TXS_BYTES_PER_BATCH {
                self.send_block_proposals(std::mem::take(&mut relay_proposals))
                    .await;
                relay_bytes = tx_size;
            } else {
                relay_bytes += tx_size;
            }
```
