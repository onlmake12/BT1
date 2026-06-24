Audit Report

## Title
Missing Duplicate-Index Deduplication and Response Byte-Size Limit in `GetBlockTransactionsProcess::execute` — (`sync/src/relayer/get_block_transactions_process.rs`)

## Summary
`GetBlockTransactionsProcess::execute` accepts up to `MAX_RELAY_TXS_NUM_PER_BATCH` (32767) transaction indexes per request without deduplicating them and without enforcing any byte-size cap on the serialized response. An attacker can send requests containing 32767 identical valid indexes, causing the node to allocate a buffer proportional to `32767 × tx_size` per request before attempting to send. With 128 peers each sending 30 req/sec, concurrent allocations can exhaust node memory and crash the process.

## Finding Description
The sole guard in `execute()` is a count check against `MAX_RELAY_TXS_NUM_PER_BATCH`: [1](#0-0) 

After this check, the handler iterates over all supplied indexes — including duplicates — and clones one `TransactionView` per index entry: [2](#0-1) 

All collected transactions are then serialized into a single flat molecule byte buffer before `async_send_message_to` is called: [3](#0-2) 

The molecule `.build()` call at line 94 serializes all 32767 transaction copies into a new contiguous allocation. For a 100 KB transaction, this produces a ~3.2 GB buffer entirely in memory before any send attempt. Even if the subsequent send fails due to the 4 MB `RelayV3` frame limit, the allocation has already been made.

The file imports only `MAX_RELAY_TXS_NUM_PER_BATCH` and has neither protection: [4](#0-3) 

The sibling handler `GetTransactionsProcess::execute` explicitly deduplicates via `HashSet` and rejects with `StatusCode::RequestDuplicate`: [5](#0-4) 

It also enforces `MAX_RELAY_TXS_BYTES_PER_BATCH` (1 MB) on the response: [6](#0-5) 

`GetBlockProposalProcess::execute` applies the same two protections: [7](#0-6) [8](#0-7) 

The rate limiter is keyed per `(PeerIndex, message.item_id())` at 30 req/sec per peer: [9](#0-8) [10](#0-9) 

With `MAX_RELAY_PEERS = 128`, the aggregate ceiling is 3840 `GetBlockTransactions` requests/sec reaching `execute()`: [11](#0-10) 

## Impact Explanation
This is a **High** severity vulnerability: it can easily crash a CKB node. An incoming `GetBlockTransactions` message with 32767 identical indexes pointing to a large stored transaction (e.g., 100 KB) causes a single `execute()` call to allocate ~3.2 GB before the send path is reached. At 3840 req/sec aggregate across 128 peers, even a fraction of concurrent in-flight calls exhausts available RAM, triggering an OOM kill or Rust allocator panic and crashing the node. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
The attack requires only 128 simultaneous TCP connections (within the default `max_peers` range of 125–500), knowledge of any stored block containing a moderately large transaction, and the ability to craft a valid molecule-encoded `GetBlockTransactions` message. No privileged access, no PoW, and no key material are needed. The block hash and transaction index are entirely public and trivially discoverable from the chain via RPC. The attack is fully scriptable, repeatable, and locally testable.

## Recommendation
Apply both fixes already present in sibling handlers:

1. **Deduplicate indexes** before processing: collect the `u32` indexes into a `HashSet<u32>`; if `set.len() != indexes.len()`, return `StatusCode::RequestDuplicate`.
2. **Enforce `MAX_RELAY_TXS_BYTES_PER_BATCH`** on the response: accumulate `tx.data().total_size()` while building the transaction list and either truncate or split the response when the 1 MB limit is reached, mirroring the pattern in `GetTransactionsProcess` and `GetBlockProposalProcess`.

## Proof of Concept
```
1. Identify any stored block hash H containing a large transaction at index 0
   (discoverable from the public chain via RPC).
2. Open 128 TCP connections to the target node on the RelayV3 protocol.
3. From each connection, send 30 GetBlockTransactions/sec with:
     block_hash = H
     indexes    = [0, 0, 0, ..., 0]  (32767 copies of index 0)
4. Each execute() call allocates ~32767 × tx_size bytes at the .build() call
   before attempting to send.
5. Monitor target node RSS; expect OOM crash within seconds.
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

**File:** sync/src/relayer/mod.rs (L59-61)
```rust
pub const MAX_RELAY_PEERS: usize = 128;
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
pub const MAX_RELAY_TXS_BYTES_PER_BATCH: usize = 1024 * 1024;
```

**File:** sync/src/relayer/mod.rs (L91-92)
```rust
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
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
