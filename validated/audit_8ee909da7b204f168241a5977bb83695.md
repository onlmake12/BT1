Audit Report

## Title
Unbounded Allocation Before Pending-Block Guard in `BlockTransactionsProcess::execute()` — (File: sync/src/relayer/block_transactions_process.rs)

## Summary
`BlockTransactionsProcess::execute()` fully deserializes and allocates all `TransactionView` and `UncleBlockView` objects from a peer-supplied `BlockTransactions` message before checking whether the referenced `block_hash` exists in `pending_compact_blocks`. A malicious peer can send crafted messages with an arbitrary number of large transactions, forcing unbounded heap allocation and CPU work that is immediately discarded when the guard check fails, with no ban penalty applied to the sender.

## Finding Description
In `sync/src/relayer/block_transactions_process.rs`, `execute()` begins at line 48 by calling `self.message.to_entity()` to produce an owned `BlockTransactions` value, then immediately iterates over every transaction and uncle to build `Vec<TransactionView>` and `Vec<UncleBlockView>` at lines 50–59:

```rust
let block_transactions = self.message.to_entity();
let block_hash = block_transactions.block_hash();
let received_transactions: Vec<core::TransactionView> = block_transactions
    .transactions()
    .into_iter()
    .map(|tx| tx.into_view())
    .collect();
let received_uncles: Vec<core::UncleBlockView> = block_transactions
    .uncles()
    .into_iter()
    .map(|uncle| uncle.into_view())
    .collect();
```

Only at line 65 does the code check whether the block is actually pending:

```rust
if let Entry::Occupied(mut pending) = shared
    .state()
    .pending_compact_blocks()
    .await
    .entry(block_hash.clone())
```

If `block_hash` is not in `pending_compact_blocks`, the function falls through to `Status::ignored()` at line 189 — all allocated memory is dropped, no ban is issued, and the attacker suffers no consequence.

There is no count check on `transactions()` or `uncles()` before the allocation loop. Every other relay handler guards count before allocating:
- `GetBlockTransactionsProcess` (lines 37–50): checks `indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH` first
- `TransactionHashesProcess` (lines 29–35): checks `tx_hashes().len() > MAX_RELAY_TXS_NUM_PER_BATCH` first
- `BlockProposalProcess` (lines 27–36): checks `transactions().len() > limit` first

`BlockTransactionsProcess` is the only relay handler missing this guard.

## Impact Explanation
A malicious peer sends repeated `BlockTransactions` messages with a random `block_hash` and the maximum number of transactions the P2P frame allows. For each message the victim node deserializes the full molecule payload, allocates a `TransactionView` per transaction and an `UncleBlockView` per uncle, checks `pending_compact_blocks`, finds nothing, and drops everything. The rate limiter permits 30 `BlockTransactions` messages per second per peer. With `MAX_RELAY_TXS_NUM_PER_BATCH = 32767`, a single attacker connection can force sustained allocation-and-free churn at the maximum permitted rate. With multiple peer connections this scales linearly. The result is elevated CPU (allocation pressure, cache thrashing) and potential memory exhaustion, degrading or halting block relay on the targeted node. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
Any peer that can establish a P2P connection — which requires no credentials — can send `BlockTransactions` messages. No prior handshake or block request is required from the attacker's side. The attack is trivially scriptable: open a connection, repeatedly send a crafted `BlockTransactions` message with a random hash and a maximum-size transaction list. The rate limiter (30/s) does not prevent the attack; it only bounds the per-peer rate, which remains sufficient to cause measurable resource consumption. The handler returns `Status::ignored()`, not a ban-worthy status, so the attacker is never disconnected or penalized.

## Recommendation
Add count guards at the top of `BlockTransactionsProcess::execute()`, before `self.message.to_entity()`, mirroring the pattern used in every other relay handler:

```rust
pub async fn execute(self) -> Status {
    let shared = self.relayer.shared();
    if self.message.transactions().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
        return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
            "BlockTransactions tx count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
            self.message.transactions().len(),
            MAX_RELAY_TXS_NUM_PER_BATCH,
        ));
    }
    if self.message.uncles().len() > shared.consensus().max_uncles_num() {
        return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
            "BlockTransactions uncle count({}) > max_uncles_num({})",
            self.message.uncles().len(),
            shared.consensus().max_uncles_num(),
        ));
    }
    // ... existing code
```

Additionally, consider checking `pending_compact_blocks` on the raw reader (before `to_entity()`) so that full deserialization is skipped entirely for unknown block hashes.

## Proof of Concept
1. Connect to a CKB node as a P2P peer using the RelayV3 protocol.
2. Construct a `BlockTransactions` molecule message with:
   - `block_hash`: any 32-byte value not corresponding to a pending compact block
   - `transactions`: fill with as many minimal `Transaction` entries as fit within the P2P frame limit
   - `uncles`: empty
3. Send this message at the maximum rate permitted by the rate limiter (30/s).
4. Observe via process monitoring (`/proc/<pid>/status`, `perf stat`) that the node's heap allocation rate and CPU usage increase proportionally to the number of transactions per message, with no corresponding useful work performed.
5. Repeat from multiple peer connections to scale the effect linearly.

The node will allocate and immediately free a `Vec<TransactionView>` for every message, with no ban or disconnect triggered, since the handler returns `Status::ignored()` for unknown block hashes. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** sync/src/relayer/block_transactions_process.rs (L48-59)
```rust
        let block_transactions = self.message.to_entity();
        let block_hash = block_transactions.block_hash();
        let received_transactions: Vec<core::TransactionView> = block_transactions
            .transactions()
            .into_iter()
            .map(|tx| tx.into_view())
            .collect();
        let received_uncles: Vec<core::UncleBlockView> = block_transactions
            .uncles()
            .into_iter()
            .map(|uncle| uncle.into_view())
            .collect();
```

**File:** sync/src/relayer/block_transactions_process.rs (L65-69)
```rust
        if let Entry::Occupied(mut pending) = shared
            .state()
            .pending_compact_blocks()
            .await
            .entry(block_hash.clone())
```

**File:** sync/src/relayer/block_transactions_process.rs (L189-189)
```rust
        Status::ignored()
```

**File:** sync/src/relayer/get_block_transactions_process.rs (L37-50)
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
```

**File:** sync/src/relayer/transaction_hashes_process.rs (L29-35)
```rust
            if relay_transaction_hashes.tx_hashes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "TxHashes count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
                    relay_transaction_hashes.tx_hashes().len(),
                    MAX_RELAY_TXS_NUM_PER_BATCH,
                ));
            }
```

**File:** sync/src/relayer/block_proposal_process.rs (L27-36)
```rust
            let limit = shared.consensus().max_block_proposals_limit()
                * (shared.consensus().max_uncles_num() as u64);
            if (block_proposals.transactions().len() as u64) > limit {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "Transactions count({}) > consensus max_block_proposals_limit({}) * max_uncles_num({})",
                    block_proposals.transactions().len(),
                    shared.consensus().max_block_proposals_limit(),
                    shared.consensus().max_uncles_num(),
                ));
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

**File:** sync/src/relayer/mod.rs (L156-165)
```rust
            packed::RelayMessageUnionReader::BlockTransactions(reader) => {
                if reader.check_data() {
                    BlockTransactionsProcess::new(reader, self, nc, peer)
                        .execute()
                        .await
                } else {
                    StatusCode::ProtocolMessageIsMalformed
                        .with_context("BlockTransactions is invalid")
                }
            }
```
