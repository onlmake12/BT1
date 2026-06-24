Audit Report

## Title
`Indexer::prune` Skips `Header`/`TxHash` Deletion When `ConsumedOutPoint` Keyspace Is Empty — (`util/indexer/src/indexer.rs`)

## Summary

`Indexer::prune` initialises `min_block_number` to `BlockNumber::MAX` and only updates it inside the `ConsumedOutPoint` deletion loop. When that keyspace is empty — which is the normal state for cellbase-only blocks — `min_block_number` remains `u64::MAX`, the RocksDB seek key for the `Header`/`TxHash` pass lands past all stored keys, and the deletion loop is silently skipped. Every `Header` and `TxHash` entry written since genesis is retained, causing unbounded storage growth in the indexer's RocksDB instance.

## Finding Description

**Write path (`append`):**

For every block that passes the block filter, `append` unconditionally writes a `Key::Header` entry (lines 496–509). `tx_matched` is set to `true` at line 438 when any output — including a cellbase output — passes the cell filter, causing a `Key::TxHash` entry to be written at lines 482–490. `Key::ConsumedOutPoint` is only written inside the `if tx_index > 0` guard (line 339), so cellbase-only blocks produce `Header` and `TxHash` entries but zero `ConsumedOutPoint` entries.

**Prune path (`prune`):**

`min_block_number` is initialised to `BlockNumber::MAX` at line 765 and is only updated at lines 777–779, inside the `ConsumedOutPoint` loop body. When the `ConsumedOutPoint` keyspace is empty (or all entries are beyond `prune_to_block`), the loop body never executes and `min_block_number` stays at `u64::MAX`. The seek key for the `Header` pass is then:

```
[KeyPrefix::Header (0xE0)] ++ [0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]
```

No stored block has block number `u64::MAX`, so the RocksDB iterator returns nothing and the entire `Header`/`TxHash` deletion loop (lines 795–805) is a no-op. Every `Header` and `TxHash` entry ever written is retained.

**Existing guards:** The `take_while` predicate at line 789–794 checks `<= prune_to_block`, which is correct in isolation, but it is never reached because the seek key already positions the iterator past all valid `Header` entries.

## Impact Explanation

`Header` and `TxHash` entries accumulate at a rate of at least one `Header` per block and one `TxHash` per cellbase transaction whose output passes the cell filter, with no upper bound. Over time, RocksDB storage grows without limit, degrading all indexer read paths (range scans, point lookups) and eventually exhausting disk on any node running the indexer. This maps to **Suboptimal implementation of CKB state storage mechanism** (Medium, 2001–10000 points).

## Likelihood Explanation

Cellbase-only blocks are the normal state of the CKB chain during low-activity periods — no special attacker capability is required. Any miner producing blocks without user transactions triggers this path. The condition is also reachable when a custom cell filter excludes all non-cellbase outputs, keeping `ConsumedOutPoint` empty even on a busy chain. The bug fires on every `prune` call under these conditions, so it is continuous and self-compounding.

## Recommendation

Replace the `min_block_number`-based seek with an unconditional start from block 0 for the `Header`/`TxHash` pruning pass:

```rust
// Replace:
let mut key_prefix_header = vec![KeyPrefix::Header as u8];
key_prefix_header.extend_from_slice(&min_block_number.to_be_bytes());

// With:
let start_number = if min_block_number == BlockNumber::MAX { 0u64 } else { min_block_number };
let mut key_prefix_header = vec![KeyPrefix::Header as u8];
key_prefix_header.extend_from_slice(&start_number.to_be_bytes());
```

This ensures `Header`/`TxHash` pruning always runs up to `prune_to_block`, regardless of whether any `ConsumedOutPoint` entries exist.

## Proof of Concept

Using the existing `new_indexer` helper at line 957 and block-building utilities in the test module:

1. Instantiate `Indexer` with `keep_num = 10`, `prune_interval = 1`.
2. Append `N = 10 * keep_num` blocks, each containing only a cellbase transaction with one output matching the default cell filter (no cell filter set).
3. After each `append`, `prune` is called automatically (since `prune_interval = 1`).
4. Count `Key::Header` and `Key::TxHash` entries in RocksDB using the statistics helper at lines 920–938.
5. **Expected (correct):** entry count stabilises at `keep_num + 1` as old entries are deleted.
6. **Actual (buggy):** count equals `N` and never decreases, because `min_block_number == u64::MAX` causes the `Header`/`TxHash` deletion loop to be skipped on every `prune` call.