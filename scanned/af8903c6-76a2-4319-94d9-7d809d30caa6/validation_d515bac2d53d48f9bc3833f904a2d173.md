### Title
Unbounded Response Allocation in `GetBlockTransactionsProcess::execute` via Duplicate Indexes with No Byte-Size Limit — (`sync/src/relayer/get_block_transactions_process.rs`)

### Summary

`GetBlockTransactionsProcess::execute` has three compounding defects that allow any unprivileged peer to force the node to allocate an unbounded `BlockTransactions` response: an off-by-one in the count guard, no deduplication of requested indexes, and a missing `MAX_RELAY_TXS_BYTES_PER_BATCH` cap that both analogous handlers (`GetTransactionsProcess`, `GetBlockProposalProcess`) enforce.

---

### Finding Description

**Defect 1 — Off-by-one in the count guard (line 37)**

The guard is:

```rust
if get_block_transactions.indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
```

`MAX_RELAY_TXS_NUM_PER_BATCH = 32767`. A message with exactly 32767 entries satisfies `32767 > 32767 == false` and is accepted. [1](#0-0) [2](#0-1) 

**Defect 2 — No deduplication of indexes**

The collection loop calls `block.transactions().get(index)` for every entry in the peer-supplied list with no deduplication:

```rust
let transactions = self
    .message
    .indexes()
    .iter()
    .filter_map(|i| block.transactions().get(Into::<u32>::into(i) as usize).cloned())
    .collect::<Vec<_>>();
```

Sending `indexes = [0u32; 32767]` causes 32767 clones of `block.transactions()[0]` (the coinbase) to be collected into a single `Vec`. [3](#0-2) 

**Defect 3 — No `MAX_RELAY_TXS_BYTES_PER_BATCH` cap on the response**

The entire `transactions` vec is packed into one `BlockTransactions` message and sent with no byte-size check:

```rust
let content = packed::BlockTransactions::new_builder()
    .block_hash(block_hash)
    .transactions(transactions.into_iter().map(|tx| tx.data()).collect::<Vec<_>>())
    ...
    .build();
let message = packed::RelayMessage::new_builder().set(content).build();
return async_send_message_to(&self.nc, self.peer, &message).await;
``` [4](#0-3) 

By contrast, both `GetTransactionsProcess` and `GetBlockProposalProcess` split their responses when `relay_bytes + tx_size > MAX_RELAY_TXS_BYTES_PER_BATCH` (1 MiB): [5](#0-4) [6](#0-5) 

---

### Impact Explanation

The peak allocation per request is `32767 × coinbase_tx_size`. CKB's consensus `max_block_bytes` is ~597 KB, so a coinbase transaction can legally be close to that size. In the worst case: `32767 × 597 KB ≈ 19.5 GB` allocated in a single async task before any send occurs. Even with a typical mainnet coinbase of ~500 bytes the allocation is ~16 MB per request. The rate limiter (30 req/s per peer) does not prevent the per-request allocation; multiple peers can issue this simultaneously. [7](#0-6) [8](#0-7) 

---

### Likelihood Explanation

The attack requires only a TCP connection to the relay protocol — no PoW, no key, no privileged role. The attacker needs a stored block hash (trivially obtained from any block explorer or by syncing headers). The message is structurally valid and passes all existing checks. The rate limiter provides partial mitigation but does not bound per-request memory. [9](#0-8) 

---

### Recommendation

1. Change the count guard to `>=` (strict upper bound inclusive):
   ```rust
   if get_block_transactions.indexes().len() >= MAX_RELAY_TXS_NUM_PER_BATCH {
   ```
2. Deduplicate indexes before lookup (as `GetTransactionsProcess` does for tx hashes).
3. Add a `MAX_RELAY_TXS_BYTES_PER_BATCH` byte-size accumulator and either truncate or split the response, matching the pattern in `GetTransactionsProcess::execute` and `GetBlockProposalProcess::execute`.

---

### Proof of Concept

```rust
// Pseudocode unit test
let indexes = vec![0u32; 32767]; // all pointing at coinbase
let msg = GetBlockTransactions { block_hash: stored_block_hash, indexes, uncle_indexes: [] };
// node has stored_block with coinbase of size N bytes
// execute() allocates 32767 * N bytes before returning
// assert serialized response size == 32767 * N (no truncation)
```

With a 1 KB coinbase: ~32 MB allocation per request.
With a 597 KB coinbase (max consensus block size): ~19.5 GB allocation per request → OOM crash. [10](#0-9)

### Citations

**File:** sync/src/relayer/get_block_transactions_process.rs (L33-51)
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
```

**File:** sync/src/relayer/get_block_transactions_process.rs (L60-97)
```rust
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
```

**File:** util/constant/src/sync.rs (L68-68)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
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
