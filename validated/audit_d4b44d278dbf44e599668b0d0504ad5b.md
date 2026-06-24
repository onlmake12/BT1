Looking at the actual code confirms the claim's core finding. Let me verify the rate limiter and the exact limit value.

Audit Report

## Title
Missing Duplicate Index Deduplication in `GetBlockTransactionsProcess::execute()` Enables Bandwidth Amplification — (File: `sync/src/relayer/get_block_transactions_process.rs`)

## Summary
`GetBlockTransactionsProcess::execute()` validates that the number of requested `indexes` does not exceed `MAX_RELAY_TXS_NUM_PER_BATCH` (32,767) but performs no deduplication on those indexes. An unprivileged connected peer can send a `GetBlockTransactions` message containing 32,767 copies of the same transaction index, causing the victim node to clone and serialize the same transaction 32,767 times and transmit all copies back in a `BlockTransactions` response. The attacker's cost is a single small message; the victim's cost is proportional to `32767 × tx_size` in CPU and outbound bandwidth.

## Finding Description
In `sync/src/relayer/get_block_transactions_process.rs`, `execute()` performs two bounds checks:

```rust
// line 37 — only checks count, not uniqueness
if get_block_transactions.indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH { … }
// line 44 — same pattern for uncle_indexes
if get_block_transactions.uncle_indexes().len() > shared.consensus().max_uncles_num() { … }
```

After passing these checks, the handler iterates over every index as-is:

```rust
// lines 61-71
let transactions = self
    .message
    .indexes()
    .iter()
    .filter_map(|i| {
        block.transactions().get(Into::<u32>::into(i) as usize).cloned()
    })
    .collect::<Vec<_>>();
```

`filter_map` with `.cloned()` returns a new `TransactionView` clone for every occurrence of a duplicate index. All clones are then serialized into a `BlockTransactions` message and sent back to the requesting peer (line 97). There is no deduplication step anywhere in this path.

`MAX_RELAY_TXS_NUM_PER_BATCH` is defined as `32767` in both `sync/src/relayer/mod.rs` (line 60) and `util/constant/src/sync.rs` (line 68). The rate limiter in `Relayer::try_process()` caps the message type at 30 requests/second per peer, but each of those 30 requests can carry 32,767 duplicate indexes, so the amplification factor per second is `30 × 32767 × tx_size`.

## Impact Explanation
This is a direct bandwidth and CPU amplification attack reachable by any connected peer with no special privileges. A single peer sending 30 crafted `GetBlockTransactions` messages per second (the rate-limit ceiling) forces the victim to serialize and transmit up to `30 × 32767` copies of the same transaction per second. For a large transaction (e.g., a cellbase with many outputs), this can saturate the victim's outbound bandwidth and CPU, degrading or severing its ability to participate in block relay. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation
Any peer that can establish a relay protocol connection can trigger this immediately. No authentication, no stake, no prior block knowledge beyond a valid block hash is required. The attack is repeatable, stateless, and can be parallelized across multiple connections (up to `MAX_RELAY_PEERS = 128`). The attacker's bandwidth cost is negligible (a few hundred bytes per request); the victim's response cost is `32767 × tx_size` bytes per request.

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

A unit test can be written in `sync/src/relayer/tests/` by constructing a mock `GetBlockTransactions` with duplicate indexes and asserting that the resulting `BlockTransactions` response contains deduplicated (not repeated) transactions.