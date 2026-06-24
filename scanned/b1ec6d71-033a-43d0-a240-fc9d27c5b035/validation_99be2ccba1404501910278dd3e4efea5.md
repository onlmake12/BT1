Audit Report

## Title
Missing Duplicate-Index Check and Byte-Size Guard in `GetBlockTransactionsProcess::execute` Enables OOM Node Crash — (`sync/src/relayer/get_block_transactions_process.rs`)

## Summary
`GetBlockTransactionsProcess::execute` enforces only a count limit on indexes but performs no deduplication and no byte-size cap on the assembled response. An attacker can send a `GetBlockTransactions` message with up to 32,767 identical indexes pointing to a large stored transaction, causing the handler to clone that transaction 32,767 times into heap memory before any send occurs. With 128 peers each sending 30 req/sec, aggregate heap pressure can exhaust node memory and trigger an OOM kill.

## Finding Description
In `get_block_transactions_process.rs`, the only structural check is a count guard:

```rust
if get_block_transactions.indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
    return StatusCode::ProtocolMessageIsMalformed...
}
``` [1](#0-0) 

After that, indexes are iterated with `filter_map` and `.cloned()` — no deduplication:

```rust
let transactions = self.message.indexes().iter()
    .filter_map(|i| block.transactions().get(Into::<u32>::into(i) as usize).cloned())
    .collect::<Vec<_>>();
``` [2](#0-1) 

If all 32,767 indexes are identical (e.g., all `0`), the `Vec` contains 32,767 clones of transaction 0. The full `BlockTransactions` message is then built entirely in memory before `async_send_message_to` is called. [3](#0-2) 

**Contrast with sibling handlers that do have guards:**

`GetTransactionsProcess::execute` explicitly rejects duplicate hashes: [4](#0-3) 

`GetBlockProposalProcess::execute` also deduplicates via `HashSet` and enforces `MAX_RELAY_TXS_BYTES_PER_BATCH`: [5](#0-4) [6](#0-5) 

`GetBlockTransactionsProcess` imports only `MAX_RELAY_TXS_NUM_PER_BATCH`, not `MAX_RELAY_TXS_BYTES_PER_BATCH`: [7](#0-6) 

The three constants are defined together: [8](#0-7) 

**Rate limiter is per-peer, not global:**

The rate limiter is keyed by `(PeerIndex, u32)` at 30 req/sec per key: [9](#0-8) 

With `MAX_RELAY_PEERS = 128`, the aggregate ceiling is 128 × 30 = 3,840 req/sec, each independently allowed. [10](#0-9) 

**Inbound frame limit does not protect against this:**

`RelayV3` caps incoming messages at 4 MB: [11](#0-10) 

A `GetBlockTransactions` message with 32,767 `uint32` indexes is only ~131 KB — well within the 4 MB inbound cap. The frame limit constrains the inbound request, not the outbound response allocation.

## Impact Explanation
This is a **High** severity finding: **Vulnerabilities which could easily crash a CKB node.**

A stored block with a large transaction (CKB's `max_block_bytes` is ~597 KB; a single transaction can occupy a significant fraction of that). With 32,767 duplicate indexes pointing to that transaction, each `execute()` call allocates `32,767 × tx_size` bytes on the heap before the send. At 3,840 concurrent calls per second, aggregate heap pressure can reach tens to hundreds of GB, crashing the node process with OOM. The attack requires no PoW, no keys, and no privileged access — only 128 TCP connections and knowledge of any stored block hash (public information).

## Likelihood Explanation
The attack is straightforward: open 128 connections, identify any large stored block hash from the public chain, and flood each connection at 30 req/sec with a `GetBlockTransactions` message containing 32,767 copies of index `0`. No special capability is required beyond network access. The rate limiter provides no meaningful protection because it is per-peer and the aggregate rate is 3,840 req/sec. The inbound frame limit does not constrain the outbound allocation.

## Recommendation
1. **Reject duplicate indexes** before processing, mirroring the guard in `GetTransactionsProcess` and `GetBlockProposalProcess`:
   ```rust
   let indexes: Vec<u32> = self.message.indexes().iter().map(Into::into).collect();
   let deduped: HashSet<u32> = indexes.iter().copied().collect();
   if deduped.len() != indexes.len() {
       return StatusCode::RequestDuplicate.with_context("duplicate indexes");
   }
   ```
2. **Enforce `MAX_RELAY_TXS_BYTES_PER_BATCH`** on the assembled transaction payload before building the response message, mirroring `GetTransactionsProcess` and `GetBlockProposalProcess`.
3. Consider a **global (cross-peer) rate limiter** or concurrency semaphore on `GetBlockTransactions` processing to bound aggregate memory pressure.

## Proof of Concept
```
1. Attacker opens 128 TCP connections to the target CKB node.
2. Attacker reads any recent block hash H from the public chain
   (block is stored on the node).
3. Each connection sends, at 30 req/sec, a RelayV3 GetBlockTransactions message:
     block_hash = H
     indexes    = [0, 0, 0, ..., 0]  // 32,767 copies of index 0
4. Each execute() call clones the cellbase (or largest tx) 32,767 times into a Vec.
5. With 128 peers × 30 req/sec = 3,840 concurrent allocations, heap exhaustion
   causes the node process to be OOM-killed.
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

**File:** sync/src/relayer/get_transactions_process.rs (L59-61)
```rust
            if message_len != tx_hashes_set.len() {
                return StatusCode::RequestDuplicate.with_context("Request duplicate transaction");
            }
```

**File:** sync/src/relayer/get_block_proposal_process.rs (L47-51)
```rust
        let proposals: HashSet<packed::ProposalShortId> =
            self.message.proposals().to_entity().into_iter().collect();

        if proposals.len() != message_len {
            return StatusCode::RequestDuplicate.with_context("Request duplicate proposal");
```

**File:** sync/src/relayer/get_block_proposal_process.rs (L84-91)
```rust
            if relay_bytes + tx_size > MAX_RELAY_TXS_BYTES_PER_BATCH {
                self.send_block_proposals(std::mem::take(&mut relay_proposals))
                    .await;
                relay_bytes = tx_size;
            } else {
                relay_bytes += tx_size;
            }
            relay_proposals.push(data);
```

**File:** sync/src/relayer/mod.rs (L59-61)
```rust
pub const MAX_RELAY_PEERS: usize = 128;
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
pub const MAX_RELAY_TXS_BYTES_PER_BATCH: usize = 1024 * 1024;
```

**File:** sync/src/relayer/mod.rs (L81-92)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}

impl Relayer {
    /// Init relay protocol handle
    ///
    /// This is a runtime relay protocol shared state, and any relay messages will be processed and forwarded by it
    pub fn new(chain: ChainController, shared: Arc<SyncShared>) -> Self {
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/support_protocols.rs (L130-130)
```rust
            SupportProtocols::RelayV3 => 4 * 1024 * 1024,     // 4   MB
```
