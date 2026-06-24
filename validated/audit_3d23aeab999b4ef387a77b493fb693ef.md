Audit Report

## Title
Missing Duplicate Index Deduplication in `GetBlockTransactionsProcess::execute()` Enables Bandwidth Amplification — (File: `sync/src/relayer/get_block_transactions_process.rs`)

## Summary
`GetBlockTransactionsProcess::execute()` validates that the number of requested `indexes` does not exceed `MAX_RELAY_TXS_NUM_PER_BATCH` (32,767) but performs no deduplication on those indexes. An unprivileged connected peer can send a `GetBlockTransactions` message containing 32,767 copies of the same transaction index, causing the victim node to clone and serialize the same transaction 32,767 times and transmit all copies back in a `BlockTransactions` response. The attacker's cost is a single small message; the victim's cost is proportional to `32767 × tx_size` in CPU and outbound bandwidth.

## Finding Description
In `sync/src/relayer/get_block_transactions_process.rs`, `execute()` performs two bounds checks: [1](#0-0) [2](#0-1) 

Neither check tests for duplicate values — only the count is validated. After passing these checks, the handler iterates over every index as-is: [3](#0-2) 

`filter_map` with `.cloned()` returns a new `TransactionView` clone for every occurrence of a duplicate index. All clones are then serialized into a `BlockTransactions` message and sent back to the requesting peer: [4](#0-3) 

There is no deduplication step anywhere in this path. `MAX_RELAY_TXS_NUM_PER_BATCH` is defined as `32767` in both locations: [5](#0-4) [6](#0-5) 

The rate limiter in `Relayer::try_process()` is keyed by `(PeerIndex, message_item_id)` and caps at 30 requests/second per peer per message type: [7](#0-6) [8](#0-7) 

Each of those 30 allowed requests per second can carry 32,767 duplicate indexes, so the amplification factor per second is `30 × 32767 × tx_size`.

## Impact Explanation
This is a direct bandwidth and CPU amplification attack reachable by any connected peer with no special privileges. A single peer sending 30 crafted `GetBlockTransactions` messages per second (the rate-limit ceiling) forces the victim to serialize and transmit up to `30 × 32767` copies of the same transaction per second. For a large transaction (e.g., a cellbase with many outputs), this can saturate the victim's outbound bandwidth and CPU, degrading or severing its ability to participate in block relay. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation
Any peer that can establish a relay protocol connection can trigger this immediately. No authentication, no stake, no prior block knowledge beyond a valid block hash is required. The attack is repeatable, stateless, and can be parallelized across multiple connections (up to `MAX_RELAY_PEERS = 128`): [9](#0-8) 

The attacker's bandwidth cost is negligible (a few hundred bytes per request); the victim's response cost is `32767 × tx_size` bytes per request.

## Recommendation
Deduplicate `indexes` and `uncle_indexes` before processing. The simplest fix is to collect into a `BTreeSet` or sort-and-dedup before the `filter_map` loop:

```rust
let mut indexes: Vec<u32> = get_block_transactions
    .indexes()
    .iter()
    .map(Into::into)
    .collect();
indexes.sort_unstable();
indexes.dedup();
if indexes.len() > MAX_RELAY_TXS_NUM_PER_BATCH { … }
```

Apply the same pattern to `uncle_indexes`. Reject (and ban) the peer if duplicates are detected, since a well-behaved client has no reason to send them.

## Proof of Concept
1. Connect a peer to a CKB node that has at least one stored block (any block after genesis).
2. Craft a `GetBlockTransactions` relay message with `block_hash` set to that block's hash and `indexes` set to `[0u32; 32767]` (32,767 copies of index 0, the cellbase).
3. Send the message over the relay protocol connection.
4. Observe the victim node's response: a `BlockTransactions` message containing 32,767 serialized copies of the cellbase transaction.
5. Repeat at 30 messages/second (the rate-limit ceiling) to sustain the amplification.

A unit test can be written in `sync/src/relayer/tests/` by constructing a mock `GetBlockTransactions` with duplicate indexes and asserting that the resulting `BlockTransactions` response contains deduplicated (not repeated) transactions. [10](#0-9)

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

**File:** sync/src/relayer/mod.rs (L59-59)
```rust
pub const MAX_RELAY_PEERS: usize = 128;
```

**File:** sync/src/relayer/mod.rs (L60-60)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
```

**File:** sync/src/relayer/mod.rs (L89-92)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
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

**File:** util/constant/src/sync.rs (L68-68)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
```
