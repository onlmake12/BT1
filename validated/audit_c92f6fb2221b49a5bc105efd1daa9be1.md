Audit Report

## Title
Zero `last_cursor` Returned on Empty `get_transactions` Result Causes Silent Pagination Restart — (File: `util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs`)

## Summary
`AsyncRichIndexerHandle::get_transactions` initializes its cursor tracking variables to integer zero in both ungrouped and grouped modes. When the query returns no rows, these zero defaults are unconditionally serialized and returned as `last_cursor`. Any caller that feeds this zero cursor back as `after` receives results starting from the very beginning of the transaction table, identical to passing `after = null`. This is inconsistent with `get_cells`, which correctly initializes `last_cursor` to an empty `Vec` and only populates it inside the row-mapping closure.

## Finding Description
**Ungrouped mode** (`group_by_transaction = None | Some(false)`):

`last_id` is initialized to `0i64` and `count` to `0i32` at lines 66–67. When the result set is empty the iterator body never executes, so both remain zero. The function then unconditionally serializes them:

```rust
let mut last_cursor = last_id.to_le_bytes().to_vec();  // [0,0,0,0,0,0,0,0]
let mut offset = count.to_le_bytes().to_vec();          // [0,0,0,0]
last_cursor.append(&mut offset);                        // 12 zero bytes
```

returning `last_cursor = JsonBytes::from_vec([0u8; 12])` inside a successful `IndexerPagination` response (lines 91–98).

When a caller feeds this back as `after`, lines 44–53 decode it to `last = 0, offset = 0` and pass `Some((0, 0))` to `get_tx_with_cell`. Lines 169–182 then emit `WHERE tx_id >= 0 OFFSET 0`. Because auto-increment `tx_id` values start at 1, the predicate is always satisfied and the query returns every matching transaction from the beginning of the table — identical to calling with `after = null`.

**Grouped mode** (`group_by_transaction = Some(true)`):

`last_cursor` is initialized to `0i64` at line 111. On an empty result it is serialized as `[0u8; 8]`. In `get_tx_with_cells`, lines 320–326 decode the cursor and emit `WHERE tx_id > 0`, which is always true for ascending order, again restarting from the first row.

**Contrast with `get_cells` (correct behavior):**

`get_cells` initializes `last_cursor` to `Vec::new()` at line 228 and only populates it inside the row-mapping closure at line 236. An empty result therefore returns an empty byte slice. Additionally, `get_cells` uses strict `>` / `<` comparisons (lines 140–141), so passing an empty cursor back would cause `decode_i64` to fail with a parse error, making incorrect usage visible rather than silently restarting.

## Impact Explanation
The bug causes repeated unnecessary full-table scans of the transaction table on the CKB node's indexer database. Any unprivileged RPC caller that paginates through `get_transactions` with a script that has no indexed transactions — or whose transactions fall outside a narrow `block_range` filter — will receive a zero cursor and, if they feed it back, trigger a full-table scan from `tx_id = 1`. As the transaction table grows, each such scan takes longer. Multiple concurrent callers can compound this effect. This matches **Low (501–2000 points): Any other important performance improvements for CKB**, as the incorrect cursor design causes avoidable and unbounded full-table SQL scans on the node.

## Likelihood Explanation
The `get_transactions` RPC is publicly documented and reachable by any unprivileged caller. The API contract does not document that `last_cursor` is meaningless on an empty result; callers reasonably assume it is safe to reuse. Filters with narrow `block_range` windows or rare scripts routinely return empty pages during normal operation. Developers who test with `get_cells` will not discover the issue because `get_cells` handles the empty case correctly. The triggering condition — an empty result page — is common in production use.

## Recommendation
Mirror the `get_cells` pattern in both modes of `get_transactions`:

1. Initialize `last_id` / `last_cursor` to `None` or an empty `Vec`, not `0`.
2. Only populate the cursor inside the row-mapping closure, so an empty result produces an empty byte slice.
3. In ungrouped mode, switch the SQL predicate from `>=` to `>` (matching `get_cells` and the grouped mode) to eliminate the need for the offset-based deduplication that makes the zero-default dangerous.
4. On the parsing side, treat an empty `after` byte slice as `None` rather than decoding it to `(0, 0)`.

## Proof of Concept
```
# 1. Call get_transactions with a script that has no indexed transactions
POST /rpc
{"method":"get_transactions","params":[
  {"script":{"code_hash":"0x0000000000000000000000000000000000000000000000000000000000000000",
             "hash_type":"data","args":"0xdeadbeef"},
   "script_type":"lock"},
  "asc","0x1",null]}

# Response: {"objects":[], "last_cursor":"0x000000000000000000000000"}  ← 12 zero bytes

# 2. Feed the zero cursor back as `after`
POST /rpc
{"method":"get_transactions","params":[
  {"script":{"code_hash":"0x0000000000000000000000000000000000000000000000000000000000000000",
             "hash_type":"data","args":"0xdeadbeef"},
   "script_type":"lock"},
  "asc","0x1","0x000000000000000000000000"]}

# Result: identical to calling with after=null — returns the first page of all
# transactions matching the script from tx_id=1, not an empty page or an error.
# Repeating step 2 indefinitely causes unbounded full-table scans.
```

The root cause lines are confirmed in the actual source:
- Ungrouped zero default: `get_transactions.rs` lines 66–67
- Grouped zero default: `get_transactions.rs` line 111
- Unsafe `>=` predicate enabling restart from offset 0: `get_transactions.rs` lines 169–182
- Correct `get_cells` pattern for comparison: `get_cells.rs` lines 228–244