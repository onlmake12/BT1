Audit Report

## Title
Missing Duplicate Index Check in `GetBlockTransactionsProcess` Allows Bandwidth Amplification - (File: `sync/src/relayer/get_block_transactions_process.rs`)

## Summary
`GetBlockTransactionsProcess::execute()` validates only the *count* of `indexes` and `uncle_indexes` against `MAX_RELAY_TXS_NUM_PER_BATCH` (32767) and `max_uncles_num`, but never checks for duplicate entries. An attacker can send a single `GetBlockTransactions` message with all 32767 slots pointing to the same large transaction, causing the node to serialize and transmit that transaction 32767 times in one response. Every other analogous relay handler in the codebase already enforces deduplication.

## Finding Description
In `get_block_transactions_process.rs`, the validation block at lines 37–50 only enforces count bounds:

```rust
if get_block_transactions.indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH { ... }
if get_block_transactions.uncle_indexes().len() > shared.consensus().max_uncles_num() { ... }
```

After passing these gates, lines 61–71 iterate over the raw (potentially duplicate) index list and collect one transaction per index entry with no deduplication:

```rust
let transactions = self.message.indexes().iter()
    .filter_map(|i| block.transactions().get(Into::<u32>::into(i) as usize).cloned())
    .collect::<Vec<_>>();
```

`MAX_RELAY_TXS_NUM_PER_BATCH` is defined as **32767** (`util/constant/src/sync.rs` line 68 and `sync/src/relayer/mod.rs` line 60). Sending `indexes = [k, k, k, …]` repeated 32767 times causes the node to build and transmit a `BlockTransactions` response containing 32767 copies of the same transaction.

By contrast, every other relay request handler already enforces uniqueness:
- `GetTransactionsProcess` (`get_transactions_process.rs` lines 54–61): deduplicates via `HashSet` and returns `StatusCode::RequestDuplicate`.
- `GetBlockProposalProcess` (`get_block_proposal_process.rs` lines 47–52): same pattern.
- `GetBlocksProcess` (`get_blocks_process.rs` lines 47–58): same pattern with a `dedup` `HashSet`.

`GetBlockTransactionsProcess` is the sole handler missing this guard.

## Impact Explanation
This is a bandwidth amplification attack matching the **High** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

With `MAX_RELAY_TXS_NUM_PER_BATCH = 32767` and the rate limiter permitting 30 `GetBlockTransactions` messages per second per peer (`sync/src/relayer/mod.rs` line 91), a single attacker peer can force the node to attempt transmitting up to `30 × 32767 × tx_size` bytes of outbound data per second. With `MAX_RELAY_PEERS = 128` (`sync/src/relayer/mod.rs` line 59), 128 coordinated peers multiply this further. The inbound cost to the attacker is negligible (a few dozen bytes per message), while the outbound cost to the victim node scales with transaction size and the full amplification factor.

## Likelihood Explanation
Any peer that has received a compact block and triggered the `GetBlockTransactions` flow can craft this message. No special privilege, key, or hashpower is required. The message passes all existing validation (count ≤ `MAX_RELAY_TXS_NUM_PER_BATCH`, `uncle_indexes` ≤ `max_uncles_num`) and reaches the response-building path unconditionally. The attacker only needs to know a valid block hash and a transaction index within that block.

## Recommendation
Add a deduplication check for both `indexes` and `uncle_indexes` immediately after the existing count checks, consistent with the pattern in `GetTransactionsProcess` and `GetBlockProposalProcess`:

```rust
let indexes = get_block_transactions.indexes();
let indexes_set: HashSet<u32> = indexes.iter().map(Into::into).collect();
if indexes_set.len() != indexes.len() {
    return StatusCode::RequestDuplicate.with_context("Duplicate transaction indexes");
}

let uncle_indexes = get_block_transactions.uncle_indexes();
let uncle_indexes_set: HashSet<u32> = uncle_indexes.iter().map(Into::into).collect();
if uncle_indexes_set.len() != uncle_indexes.len() {
    return StatusCode::RequestDuplicate.with_context("Duplicate uncle indexes");
}
```

## Proof of Concept
1. Connect to a CKB node as a relay peer.
2. Obtain a block hash for a block containing a large transaction at index `k`.
3. Send a `GetBlockTransactions` message with `block_hash` set to the target block hash, `indexes` set to `[k]` repeated 32767 times, and `uncle_indexes` set to `[]`.
4. Observe that the node responds with a `BlockTransactions` message containing 32767 copies of the same transaction, with outbound bytes ≈ `32767 × tx_size` for an inbound request of only a few dozen bytes.
5. Repeat at 30 messages/second (the rate limit) to sustain the amplification continuously.