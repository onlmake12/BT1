Audit Report

## Title
`prune()` Skips Header and TxHash Pruning When No ConsumedOutPoint Entries Exist — (`util/indexer/src/indexer.rs`)

## Summary

The `prune()` function derives its `Header`/`TxHash` iterator seek key exclusively from `min_block_number`, which is only updated when `ConsumedOutPoint` entries exist. For cellbase-only blocks, no `ConsumedOutPoint` entries are ever written, so `min_block_number` stays at `u64::MAX`, causing the iterator to seek past all real `Header` keys and immediately terminate. As a result, `TxHash => TransactionInputs` and `Header => Transactions` entries for old blocks are never deleted, growing without bound proportional to chain length.

## Finding Description

In `prune()` at line 765, `min_block_number` is initialized to `BlockNumber::MAX`. The loop at lines 766–781 only assigns a real block number if at least one `ConsumedOutPoint` entry with `block_number < prune_to_block` is found. If the loop body never executes (no such entries), `min_block_number` remains `u64::MAX`.

At line 785, `key_prefix_header` is extended with `min_block_number.to_be_bytes()` = `[0xFF; 8]`, producing seek key `[224u8, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]` — the very end of the `Header` key space. The `take_while` guard at line 793 (`block_number <= prune_to_block`) immediately terminates because no real blocks exist at block `u64::MAX`. Neither `Header` nor `TxHash` entries are deleted.

The root cause of absent `ConsumedOutPoint` entries for cellbase-only blocks: in `append()`, the input-processing loop is gated on `tx_index > 0` (line 339), so cellbase transactions (always `tx_index == 0`) never produce `ConsumedOutPoint` entries. However, cellbase outputs ARE processed at line 424 onward — if any output matches the cell filter, `tx_matched = true` (line 438), causing a `TxHash` entry to be written (lines 482–490) and a `Header` entry to be written (lines 494–510). The existing `prune_bound` test (lines 1532–1600) exercises exactly this scenario (21 cellbase-only blocks with matching outputs) but only asserts `get_block_hash` succeeds — it does not assert that old `TxHash`/`Header` entries are absent after pruning.

## Impact Explanation

`TxHash => TransactionInputs` (prefix `192`) and `Header => Transactions` (prefix `224`) entries accumulate every block and are never compacted from the indexer's RocksDB store. This constitutes a suboptimal implementation of CKB's state storage mechanism: the indexer's disk usage grows without bound proportional to chain length, and read/scan performance degrades over time. This matches **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism**.

## Likelihood Explanation

Cellbase-only blocks are a normal condition during low-activity periods on mainnet — any miner producing valid blocks triggers this path. No attacker capability or special privilege is required. The default indexer configuration (no cell filter) matches all outputs including cellbase outputs, so the bug is triggered on any standard deployment. The condition is continuously reachable and self-compounding: every new block without non-cellbase transactions adds unremovable entries.

## Recommendation

Decouple the `Header`/`TxHash` pruning start key from `min_block_number`. When `min_block_number == BlockNumber::MAX` (no `ConsumedOutPoint` entries were pruned), the iterator should start from block `0` rather than `u64::MAX`:

```rust
let start_block = if min_block_number == BlockNumber::MAX {
    0u64
} else {
    min_block_number
};
let mut key_prefix_header = vec![KeyPrefix::Header as u8];
key_prefix_header.extend_from_slice(&start_block.to_be_bytes());
```

Additionally, extend the `prune_bound` test to assert that `TxHash` and `Header` entries for blocks `<= tip - keep_num - 1` are absent after pruning.

## Proof of Concept

1. Create an `Indexer` with `keep_num = 10`, `prune_interval = 1`.
2. Append 12 blocks, each containing only a cellbase transaction with one output matching the cell filter (no non-cellbase txs) — identical to the `prune_bound` test setup.
3. After the final `append()` (which calls `prune()` automatically), scan all keys with prefix `KeyPrefix::TxHash` (`192`) and `KeyPrefix::Header` (`224`).
4. Assert that entries for blocks `<= tip - keep_num - 1` (i.e., blocks 0 and 1) are absent.
5. The assertion will fail: entries for those blocks remain, demonstrating the bug. The `prune_bound` test at line 1534 already sets up this exact scenario but omits this assertion, confirming the gap.