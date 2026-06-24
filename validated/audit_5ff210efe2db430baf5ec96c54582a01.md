Audit Report

## Title
`prune()` Omits Deletion of `TxLockScript` and `TxTypeScript` Index Entries, Causing Unbounded Storage Growth — (`File: util/indexer/src/indexer.rs`)

## Summary

The `prune()` function in the CKB indexer deletes `ConsumedOutPoint` (prefix 32), `TxHash` (prefix 192), and `Header` (prefix 224) entries for blocks older than `tip - keep_num`, but never deletes the corresponding `TxLockScript` (prefix 128) and `TxTypeScript` (prefix 160) entries. These entries are written for every transaction input and output during `append()` and are correctly deleted during `rollback()`, but the asymmetric omission in `prune()` causes them to accumulate indefinitely in RocksDB, defeating the storage-bounding purpose of `keep_num`.

## Finding Description

During `append()`, for every spent cell input the indexer writes:
- `Key::TxLockScript(script, block_number, tx_index, input_index, Input)` → `TxHash` (line 384–393)
- `Key::TxTypeScript(script, block_number, tx_index, input_index, Input)` → `TxHash` (line 404–413, if type script present)

And for every cell output:
- `Key::TxLockScript(script, block_number, tx_index, output_index, Output)` → `TxHash` (line 447–456)
- `Key::TxTypeScript(script, block_number, tx_index, output_index, Output)` → `TxHash` (line 462–471, if type script present)

The `rollback()` function correctly deletes all four of these entry types when unwinding a block (lines 571–595 for outputs, lines 639–668 for inputs), demonstrating that the deletion logic is known and implementable.

The `prune()` function (lines 752–810) only issues deletes for:
1. `ConsumedOutPoint` keys (lines 760–781)
2. `TxHash` keys (line 802)
3. `Header` keys (line 804)

There is no corresponding deletion of `TxLockScript` or `TxTypeScript` keys anywhere in `prune()`. The schema comment at line 34 and 39 explicitly marks only `ConsumedOutPoint` and `TxHash` as `* rollback and prune`, confirming the omission is not intentional design but a gap.

Because `TxLockScript`/`TxTypeScript` keys embed the full script bytes plus `block_number + tx_index + cell_index + cell_type` (17 bytes of positional data), every transaction in every pruned block leaves at least one permanent `TxLockScript` entry and optionally one `TxTypeScript` entry per output and per spent input. These are never reclaimed regardless of `keep_num`.

## Impact Explanation

This is a **suboptimal implementation of the CKB state storage mechanism** (Medium, 2001–10000 points). The `keep_num` configuration parameter is the operator-facing control for bounding indexer disk usage. The `prune()` function is the enforcement mechanism. Because `TxLockScript` and `TxTypeScript` entries — which embed full script bytes and grow proportionally to chain activity — are never pruned, the RocksDB store grows without bound even when `keep_num` is set to a small value. On a busy chain with many type-script-bearing cells, this directly undermines the storage guarantees the indexer is designed to provide.

Additionally, `get_transactions` (service.rs line 381) iterates over `TxLockScript`/`TxTypeScript` entries to serve RPC results. After pruning, these stale entries remain and are returned to callers, causing the RPC to surface transaction references from blocks older than `keep_num` — inconsistent with the pruned state of the rest of the index.

## Likelihood Explanation

`prune()` is called automatically every `prune_interval` blocks during normal `append()` operation (line 517–519). Any node running the indexer with a non-zero `keep_num` is affected. The accumulation is proportional to chain activity: each transaction contributes at least one `TxLockScript` entry per output and per spent input. The effect is observable and measurable by any operator inspecting RocksDB key counts by prefix.

## Recommendation

In `prune()`, before deleting `ConsumedOutPoint` entries, read the cell output data (lock script and optional type script) from each `ConsumedOutPoint` value using `Value::parse_cell_value`, then reconstruct and batch-delete the corresponding `Key::TxLockScript(..., CellType::Input)` and `Key::TxTypeScript(..., CellType::Input)` keys. For output-side `TxLockScript`/`TxTypeScript` entries (written during `append()` for cell outputs), iterate the `Header` value's transaction list and reconstruct the output-side keys using the stored `outputs_len`. The deletion pattern already exists verbatim in `rollback()` (lines 571–595 and 639–668) and should be mirrored in `prune()`.

## Proof of Concept

1. Start a CKB node with the indexer enabled and `keep_num = 10`, `prune_interval = 1`.
2. Index 30+ blocks containing transactions that spend cells with lock scripts and type scripts.
3. After block 21, `prune()` fires and deletes `ConsumedOutPoint` and `TxHash` entries for blocks 0–10.
4. Scan RocksDB directly for keys with prefix byte `0x80` (128, `TxLockScript`) and `0xA0` (160, `TxTypeScript`): entries for blocks 0–10 are still present.
5. Scan for keys with prefix byte `0x20` (32, `ConsumedOutPoint`) and `0xC0` (192, `TxHash`): entries for blocks 0–10 are absent.
6. Continue indexing to block 10,000: the `TxLockScript`/`TxTypeScript` prefix in RocksDB grows linearly with chain activity while `ConsumedOutPoint` remains bounded by `keep_num`, demonstrating the asymmetric unbounded growth.