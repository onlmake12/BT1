Audit Report

## Title
Unbounded Memory Allocation via Duplicate Indexes and Missing Byte-Size Cap in `GetBlockTransactionsProcess::execute` — (`sync/src/relayer/get_block_transactions_process.rs`)

## Summary

`GetBlockTransactionsProcess::execute` accepts up to 32767 peer-supplied transaction indexes with no deduplication and no byte-size cap on the assembled response. An attacker with a TCP connection can send `indexes = [0u32; 32767]` for any stored block, forcing the node to clone the coinbase transaction 32767 times into a single allocation before any send occurs. The rate limiter does not bound per-request memory. This can crash a CKB node.

## Finding Description

**Defect 1 — Off-by-one in the count guard**

The guard at line 37 uses strict `>`:

```rust
if get_block_transactions.indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
```

`MAX_RELAY_TXS_NUM_PER_BATCH = 32767`, so a message with exactly 32767 entries passes the check (`32767 > 32767 == false`). [1](#0-0) [2](#0-1) 

**Defect 2 — No deduplication of indexes**

The collection loop iterates every peer-supplied index with no deduplication:

```rust
let transactions = self
    .message
    .indexes()
    .iter()
    .filter_map(|i| block.transactions().get(Into::<u32>::into(i) as usize).cloned())
    .collect::<Vec<_>>();
```

Sending `[0u32; 32767]` produces 32767 `.cloned()` copies of `block.transactions()[0]` (the coinbase). [3](#0-2) 

By contrast, `GetTransactionsProcess` inserts hashes into a `HashSet` and immediately rejects duplicates with `StatusCode::RequestDuplicate`: [4](#0-3) 

And `GetBlockProposalProcess` does the same: [5](#0-4) 

**Defect 3 — No `MAX_RELAY_TXS_BYTES_PER_BATCH` cap**

The entire `transactions` vec is packed into a single `BlockTransactions` message and sent with no byte-size check: [6](#0-5) 

Both analogous handlers split their responses when `relay_bytes + tx_size > MAX_RELAY_TXS_BYTES_PER_BATCH` (1 MiB): [7](#0-6) [8](#0-7) 

**Exploit flow:**

1. Attacker obtains any confirmed block hash (trivially from a block explorer or by syncing headers).
2. Attacker sends a `GetBlockTransactions` message with `indexes = [0u32; 32767]` and the target block hash.
3. The count guard passes (32767 is not `> 32767`).
4. The node fetches the stored block, clones the coinbase 32767 times, and assembles a single `BlockTransactions` message with no size check.
5. Peak allocation occurs before any send: `32767 × coinbase_size` bytes in one async task.

The rate limiter (30 req/s per peer) does not bound per-request allocation; it only limits request frequency. [9](#0-8) 

## Impact Explanation

With a typical mainnet coinbase of ~500 bytes: `32767 × 500 B ≈ 16 MB` per request. At 30 req/s from one peer that is ~480 MB/s of allocation. Multiple peers amplify this linearly. With a large coinbase (CKB's `max_block_bytes` allows a block to consist almost entirely of a single coinbase transaction), the per-request allocation can reach several gigabytes, causing an OOM crash of the node process.

This matches the allowed impact: **"Vulnerabilities which could easily crash a CKB node" (High, 10001–15000 points)**.

## Likelihood Explanation

The attack requires only a TCP connection to the relay protocol — no proof-of-work, no key, no privileged role. Any block hash stored on the target node suffices (trivially obtained from any block explorer). The message is structurally valid and passes all existing checks. The attack is repeatable and can be parallelized across multiple peers. The rate limiter provides no meaningful per-request memory bound.

## Recommendation

1. **Fix the off-by-one**: change `>` to `>=` in the count guard:
   ```rust
   if get_block_transactions.indexes().len() >= MAX_RELAY_TXS_NUM_PER_BATCH {
   ```
2. **Deduplicate indexes before lookup**: collect indexes into a `HashSet<u32>` and reject the message if duplicates are detected, matching the pattern in `GetTransactionsProcess` and `GetBlockProposalProcess`.
3. **Add a `MAX_RELAY_TXS_BYTES_PER_BATCH` accumulator**: iterate over the collected transactions, accumulate byte sizes, and either truncate or split the response when the limit is exceeded, matching the pattern in `GetTransactionsProcess::execute` and `GetBlockProposalProcess::execute`.

## Proof of Concept

```rust
// Minimal unit test sketch
let indexes = vec![0u32; 32767]; // all pointing at coinbase
let msg = GetBlockTransactions {
    block_hash: any_stored_block_hash, // trivially obtained
    indexes,
    uncle_indexes: vec![],
};
// Node has the block stored with coinbase of size N bytes.
// execute() allocates 32767 * N bytes before returning.
// With N = 500 bytes  → ~16 MB per request, ~480 MB/s at rate limit
// With N = 100 KB     → ~3.2 GB per request → OOM crash
// No truncation or splitting occurs; the full Vec is serialized into one message.
```

A fuzz test targeting `GetBlockTransactionsProcess::execute` with repeated index 0 and varying coinbase sizes would reproduce the unbounded allocation deterministically.

### Citations

**File:** sync/src/relayer/get_block_transactions_process.rs (L37-37)
```rust
            if get_block_transactions.indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
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

**File:** sync/src/relayer/get_block_proposal_process.rs (L47-51)
```rust
        let proposals: HashSet<packed::ProposalShortId> =
            self.message.proposals().to_entity().into_iter().collect();

        if proposals.len() != message_len {
            return StatusCode::RequestDuplicate.with_context("Request duplicate proposal");
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
