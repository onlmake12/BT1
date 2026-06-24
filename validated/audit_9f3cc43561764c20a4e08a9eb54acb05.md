Audit Report

## Title
Missing Duplicate Index Check in `GetBlockTransactionsProcess::execute()` Enables Bandwidth Amplification - (File: `sync/src/relayer/get_block_transactions_process.rs`)

## Summary
`GetBlockTransactionsProcess::execute()` validates only the *count* of `indexes` and `uncle_indexes` against `MAX_RELAY_TXS_NUM_PER_BATCH` (32767) but never checks for duplicate entries. An unprivileged peer can craft a single `GetBlockTransactions` message with all 32767 slots pointing to the same large transaction, causing the serving node to serialize and transmit that transaction 32767 times in one `BlockTransactions` response. Every other analogous relay handler already enforces a deduplication guard; this handler is the sole exception.

## Finding Description
In `sync/src/relayer/get_block_transactions_process.rs`, `execute()` performs two count-only guards:

```rust
if get_block_transactions.indexes().len() > MAX_RELAY_TXS_NUM_PER_BATCH { … }
if get_block_transactions.uncle_indexes().len() > shared.consensus().max_uncles_num() { … }
```

After passing these gates, the handler iterates the raw (potentially duplicate) index list directly:

```rust
let transactions = self.message.indexes().iter()
    .filter_map(|i| block.transactions().get(Into::<u32>::into(i) as usize).cloned())
    .collect::<Vec<_>>();
```

`MAX_RELAY_TXS_NUM_PER_BATCH` is defined as `32767` in both `util/constant/src/sync.rs` (line 68) and `sync/src/relayer/mod.rs` (line 60). Unlike `get_transactions_process.rs` (lines 54–61) and `get_block_proposal_process.rs` (lines 47–52), which both build a `HashSet` and return `StatusCode::RequestDuplicate` on collision, and `get_blocks_process.rs` (lines 47–58), which uses a `dedup` `HashSet` inline, `GetBlockTransactionsProcess` has no such guard. Additionally, unlike `GetTransactionsProcess` and `GetBlockProposalProcess`, this handler applies no `MAX_RELAY_TXS_BYTES_PER_BATCH` cap on the outbound payload, so the response size is bounded only by count × transaction size.

## Impact Explanation
An attacker sends one small inbound message (~tens of bytes) containing 32767 identical indexes pointing to the largest transaction in a known block. The node serializes and transmits 32767 copies of that transaction in a single outbound `BlockTransactions` message, with no byte-budget check. The rate limiter (30 relay messages/second/peer, `mod.rs` line 91) allows 30 such amplified responses per second per peer. Multiple coordinated peers compound the effect linearly. This maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, as the attacker's cost is negligible (a single peer connection and a crafted message) while the victim node's outbound bandwidth is saturated.

## Likelihood Explanation
Any peer that has received a compact block and entered the `GetBlockTransactions` flow can craft this message. No special privilege, key, or hashpower is required. The message passes all existing validation (count ≤ 32767, uncle_indexes ≤ `max_uncles_num`) and reaches the response-building path unconditionally. The attack is repeatable at 30 messages/second per peer and is trivially scriptable.

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
2. Identify a block containing a large transaction at index `k` (e.g., a transaction near the block size limit).
3. Send a `GetBlockTransactions` message with `block_hash` set to that block's hash, `indexes` set to `[k; 32767]` (32767 copies of `k`), and `uncle_indexes` set to `[]`.
4. Observe the node responds with a `BlockTransactions` message containing 32767 copies of the same transaction. Measure outbound bytes ≈ `32767 × tx_size` for an inbound request of ~tens of bytes.
5. Repeat at 30 messages/second (within the rate limit) to sustain bandwidth exhaustion. Add additional peers to scale the attack linearly.