### Title
Missing Duplicate-Index Check and Byte-Size Guard in `GetBlockTransactionsProcess::execute` Enables Multi-Peer OOM Amplification — (`sync/src/relayer/get_block_transactions_process.rs`)

---

### Summary

`GetBlockTransactionsProcess::execute` enforces only a count limit on indexes (`MAX_RELAY_TXS_NUM_PER_BATCH = 32767`) but performs **no deduplication** of those indexes and **no byte-size cap** on the assembled response. Because the rate limiter is keyed per `(PeerIndex, message_item_id)` independently, 128 peers each sending 30 req/sec can drive 3,840 concurrent `execute()` calls, each cloning up to 32,767 copies of the same large stored transaction into heap memory before any send occurs.

---

### Finding Description

**Root cause — no duplicate index rejection:**

In `get_block_transactions_process.rs`, the only structural check is:

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

If all 32,767 indexes are identical (e.g., all `0`), the Vec contains 32,767 clones of transaction 0. The serialized `BlockTransactions` message is then built entirely in memory before `async_send_message_to` is called. [3](#0-2) 

**Contrast with sibling handlers that do have guards:**

`GetTransactionsProcess::execute` explicitly rejects duplicate hashes:

```rust
if message_len != tx_hashes_set.len() {
    return StatusCode::RequestDuplicate.with_context("Request duplicate transaction");
}
``` [4](#0-3) 

And both `GetTransactionsProcess` and `GetBlockProposalProcess` enforce `MAX_RELAY_TXS_BYTES_PER_BATCH` (1 MB) on the outgoing payload before sending: [5](#0-4) [6](#0-5) 

`GetBlockTransactionsProcess` imports neither guard.

**Rate limiter is per-peer, not global:**

The `Relayer` rate limiter is keyed by `(PeerIndex, u32)` at 30 req/sec per key: [7](#0-6) 

With `MAX_RELAY_PEERS = 128`, the aggregate ceiling is 128 × 30 = **3,840 concurrent `execute()` calls per second**, each independently allowed. [8](#0-7) 

**Inbound frame limit does not protect against this:**

`RelayV3` caps incoming messages at 4 MB: [9](#0-8) 

A `GetBlockTransactions` message with 32,767 `uint32` indexes is only ~128 KB — well within the 4 MB inbound cap. The frame limit does not constrain the outbound response allocation.

---

### Impact Explanation

A stored block with a large transaction (CKB's block byte limit is consensus-bounded but can be hundreds of KB per transaction). With 32,767 duplicate indexes pointing to that transaction, each `execute()` call allocates `32,767 × tx_size` bytes on the heap before the send. At 3,840 concurrent calls, aggregate heap pressure can reach tens to hundreds of GB, crashing the node with OOM. The attack requires no PoW, no keys, and no privileged access — only 128 TCP connections and knowledge of any stored block hash (public information).

---

### Likelihood Explanation

The attack is straightforward to mount: open 128 connections, identify any large stored block hash from the public chain, and flood each connection at 30 req/sec with a `GetBlockTransactions` message containing 32,767 copies of index `0`. No special capability is required beyond network access.

---

### Recommendation

1. **Reject duplicate indexes** before processing, mirroring the guard in `GetTransactionsProcess`:
   ```rust
   let deduped: HashSet<u32> = indexes.iter().map(Into::into).collect();
   if deduped.len() != indexes.len() {
       return StatusCode::RequestDuplicate.with_context("duplicate indexes");
   }
   ```
2. **Enforce `MAX_RELAY_TXS_BYTES_PER_BATCH`** on the assembled transaction payload before building the response message, mirroring `GetTransactionsProcess` and `GetBlockProposalProcess`.
3. Consider a **global (cross-peer) rate limiter** or a concurrency semaphore on `GetBlockTransactions` processing to bound aggregate memory pressure.

---

### Proof of Concept

```
1. Attacker opens 128 TCP connections to the target CKB node.
2. Attacker reads any recent block hash H from the public chain (block is stored on the node).
3. Each connection sends, at 30 req/sec, a RelayV3 GetBlockTransactions message:
     block_hash = H
     indexes    = [0, 0, 0, ..., 0]  // 32,767 copies of index 0
4. Each execute() call clones the cellbase (or largest tx) 32,767 times into a Vec.
5. With 128 peers × 30 req/sec = 3,840 concurrent allocations, heap exhaustion
   causes the node process to be OOM-killed.
``` [10](#0-9)

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

**File:** sync/src/relayer/get_transactions_process.rs (L59-61)
```rust
            if message_len != tx_hashes_set.len() {
                return StatusCode::RequestDuplicate.with_context("Request duplicate transaction");
            }
```

**File:** sync/src/relayer/get_transactions_process.rs (L90-96)
```rust
                if relay_bytes + tx.total_size() > MAX_RELAY_TXS_BYTES_PER_BATCH {
                    self.send_relay_transactions(std::mem::take(&mut relay_txs))
                        .await;
                    relay_bytes = tx.total_size();
                } else {
                    relay_bytes += tx.total_size();
                }
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
