Audit Report

## Title
Duplicate Indexes in `GetBlockTransactions` Bypass Uniqueness Check, Enabling Bandwidth Amplification Attack — (File: `sync/src/relayer/get_block_transactions_process.rs`)

## Summary
`GetBlockTransactionsProcess::execute()` checks only the raw length of the `indexes` array against `MAX_RELAY_TXS_NUM_PER_BATCH` (32767) but performs no deduplication. An attacker can send a `GetBlockTransactions` message with 32767 identical indexes pointing to the same transaction, causing the victim node to fetch and serialize that transaction 32767 times and transmit the entire payload in a single `BlockTransactions` response — with no outbound size cap applied in this code path.

## Finding Description

**Root cause — `get_block_transactions_process.rs` lines 37–78:**

```rust
if get_block_transactions.indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
    return StatusCode::ProtocolMessageIsMalformed ...
}
// No deduplication check follows
let transactions = self
    .message
    .indexes()
    .iter()
    .filter_map(|i| block.transactions().get(Into::<u32>::into(i) as usize).cloned())
    .collect::<Vec<_>>();
// All 32767 copies of the same tx are collected and sent in one message
``` [1](#0-0) 

The count guard uses the raw (pre-dedup) length, so `[0, 0, 0, …, 0]` (32767 entries) passes the check. The `filter_map` then resolves each index independently against the stored block, producing 32767 identical `TransactionView` clones. These are serialized into a single `BlockTransactions` message and sent with no `MAX_RELAY_TXS_BYTES_PER_BATCH` guard — that guard exists only in `get_transactions_process.rs` and `get_block_proposal_process.rs`, not here. [2](#0-1) 

**Contrast with handlers that do check for duplicates:**

- `GetTransactionsProcess` builds a `HashSet` and returns `RequestDuplicate` if `message_len != set.len()`.
- `GetBlockProposalProcess` does the same with a `HashSet<ProposalShortId>`. [3](#0-2) [4](#0-3) 

`GetBlockTransactionsProcess` has no equivalent guard.

**Rate limiter context:** The relay rate limiter allows 30 messages/second per peer per message type. [5](#0-4) 

With 30 requests/second, each carrying 32767 duplicate indexes to a 1 KB transaction, the victim node emits ≈ 30 × 32 MB = ~960 MB/s of outbound traffic per attacker connection. `MAX_RELAY_PEERS` is 128, so the theoretical ceiling is much higher. [6](#0-5) 

## Impact Explanation

**High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An unprivileged peer can force a victim full node to generate and transmit multi-megabyte responses per request at 30 req/s, exhausting the node's outbound bandwidth and degrading its ability to serve legitimate peers, propagate blocks, and relay transactions. This directly maps to the "network congestion with few costs" impact class.

## Likelihood Explanation

Any connected peer can trigger this immediately. No special privilege, no leaked key, no victim mistake required. The attacker only needs to know any valid block hash stored by the victim (trivially obtained from the public chain). The attack is repeatable, stateless, and cheap — a single low-bandwidth connection suffices to saturate the victim's uplink.

## Recommendation

Add a deduplication check in `GetBlockTransactionsProcess::execute()` immediately after the length check, mirroring the pattern used in `GetTransactionsProcess` and `GetBlockProposalProcess`:

```rust
let indexes_set: HashSet<u32> = self.message.indexes().iter()
    .map(Into::<u32>::into).collect();
if indexes_set.len() != self.message.indexes().len() {
    return StatusCode::RequestDuplicate.with_context("Duplicate transaction index");
}

let uncle_set: HashSet<u32> = self.message.uncle_indexes().iter()
    .map(Into::<u32>::into).collect();
if uncle_set.len() != self.message.uncle_indexes().len() {
    return StatusCode::RequestDuplicate.with_context("Duplicate uncle index");
}
```

Additionally, apply a `MAX_RELAY_TXS_BYTES_PER_BATCH` guard on the outbound `BlockTransactions` response, consistent with the batching logic in `get_transactions_process.rs`.

## Proof of Concept

**Minimal manual steps:**

1. Connect a custom peer to a CKB full node via the RelayV3 protocol.
2. Identify any stored block hash `H` and a valid transaction index `i` within that block.
3. Construct a `GetBlockTransactions` message: `block_hash = H`, `indexes = [i; 32767]`.
4. Send the message. Observe the victim node transmitting a `BlockTransactions` response containing 32767 copies of the same transaction.
5. Repeat at 30 req/s (within the rate limit) and measure outbound bandwidth on the victim.

**Unit test plan** (mirrors existing `test_duplicate` tests in `sync/src/relayer/tests/`):

```rust
#[test]
fn test_duplicate_indexes() {
    // build a stored block with at least one transaction
    // send GetBlockTransactions with indexes = [0u32; 32767]
    // assert the response BlockTransactions contains 32767 identical transactions
    // OR assert StatusCode::RequestDuplicate is returned after the fix
}
```

### Citations

**File:** sync/src/relayer/get_block_transactions_process.rs (L37-97)
```rust
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

**File:** sync/src/relayer/get_block_proposal_process.rs (L47-52)
```rust
        let proposals: HashSet<packed::ProposalShortId> =
            self.message.proposals().to_entity().into_iter().collect();

        if proposals.len() != message_len {
            return StatusCode::RequestDuplicate.with_context("Request duplicate proposal");
        }
```

**File:** sync/src/relayer/mod.rs (L59-61)
```rust
pub const MAX_RELAY_PEERS: usize = 128;
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
pub const MAX_RELAY_TXS_BYTES_PER_BATCH: usize = 1024 * 1024;
```

**File:** sync/src/relayer/mod.rs (L89-92)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```
