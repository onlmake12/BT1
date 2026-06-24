Audit Report

## Title
Attacker-Controlled SQL OFFSET in `get_tx_with_cell` Enables Unbounded O(n) Database Scan via Crafted Pagination Cursor — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs`)

## Summary
When `group_by_transaction` is `false` or absent, `get_transactions` decodes the last 4 bytes of the `after` cursor as an i32 SQL OFFSET with no upper-bound validation. An unprivileged caller can craft a 12-byte cursor with `offset = i32::MAX` (2,147,483,647), forcing the database to materialize and discard billions of rows before returning results. Repeated requests constitute a sustained denial-of-service against the indexer RPC.

## Finding Description
In `get_transactions.rs` lines 44–54, the `after` cursor is validated only for length (must be exactly 12 bytes), then split: the first 8 bytes become a `last_id` (i64) and the final 4 bytes become an `offset` (i32) via `decode_i32`:

```rust
if after.len() != 12 {
    return Err(...);
}
let (last, offset) = after.as_bytes().split_at(after.len() - 4);
let last = decode_i64(last)?;
let offset = decode_i32(offset)?;
last_cursor = Some((last, offset));
```

`decode_i32` in `mod.rs` lines 286–294 performs **only a length check**, no range validation:

```rust
fn decode_i32(data: &[u8]) -> Result<i32, Error> {
    if data.len() != 4 { return Err(...); }
    Ok(i32::from_le_bytes(to_fixed_array(&data[0..4])))
}
```

This decoded offset is passed verbatim to the SQL engine in `get_transactions.rs` lines 179–182:

```rust
query_builder.limit(limit);
if let Some((_, offset)) = last_cursor {
    query_builder.offset(offset);
}
```

The `request_limit` guard at lines 23–32 caps only the `limit` (number of returned rows), not the `offset`. SQL `OFFSET N` requires the database to materialize and discard N rows before returning results. With `offset = i32::MAX`, every such request forces a full sequential scan of the entire filtered result set. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

## Impact Explanation
The rich-indexer RPC endpoint (`get_transactions`) becomes effectively unresponsive under repeated crafted requests. Each request with a large offset saturates the database thread for an extended period, blocking all concurrent legitimate indexer queries. This maps to **Note (0–500 points): Any local RPC API crash** — the indexer RPC API is rendered non-functional without crashing the process. The impact is confined to the indexer service and does not affect CKB node consensus, P2P networking, or block production.

## Likelihood Explanation
The rich-indexer is a documented, production-enabled feature. The RPC endpoint is unauthenticated by default. The cursor format is trivially derivable: any real `last_cursor` returned by `get_transactions` is 12 bytes, and the attacker need only replace the last 4 bytes with `\xff\xff\xff\x7f` (i32::MAX in little-endian). No special privileges, keys, or chain access are required. The exploit is repeatable at will.

## Recommendation
Add an upper-bound check on the decoded offset immediately after decoding, before it is used in the query. A legitimate server-generated cursor will always have `offset <= limit`, so any cursor with a larger offset is either malformed or malicious:

```rust
let offset = decode_i32(offset)?;
if offset as u32 > limit {
    return Err(Error::Params("Invalid cursor: offset out of range".to_string()));
}
```

A more robust long-term fix is to replace the OFFSET-based cursor with a keyset pagination scheme using `(last_tx_id, last_io_index)`, making pagination O(1) regardless of position.

## Proof of Concept
1. Enable rich-indexer. Index a chain with a lock script appearing in many transaction inputs/outputs.
2. Call `get_transactions` with `group_by_transaction=false`, `limit=1` to obtain a valid 12-byte `last_cursor`.
3. Craft a new `after` value: take any 8 bytes for `last_id` (e.g., `\x01\x00\x00\x00\x00\x00\x00\x00`), append `\xff\xff\xff\x7f` (i32::MAX LE) as the offset bytes.
4. Submit `get_transactions` with this crafted `after` cursor.
5. Observe that query latency grows proportionally to the offset value. The 12-byte length check at line 45 passes, `decode_i32` succeeds, and `query_builder.offset(2147483647)` is emitted into the SQL, forcing the DB to skip over two billion rows before returning results.

### Citations

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L23-32)
```rust
        let limit = limit.value();
        if limit == 0 {
            return Err(Error::invalid_params("limit should be greater than 0"));
        }
        if limit as usize > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
        }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L44-54)
```rust
                if let Some(after) = after {
                    if after.len() != 12 {
                        return Err(Error::Params(
                            "Unable to parse the 'after' parameter.".to_string(),
                        ));
                    }
                    let (last, offset) = after.as_bytes().split_at(after.len() - 4);
                    let last = decode_i64(last)?;
                    let offset = decode_i32(offset)?;
                    last_cursor = Some((last, offset));
                };
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L179-182)
```rust
    query_builder.limit(limit);
    if let Some((_, offset)) = last_cursor {
        query_builder.offset(offset);
    }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L286-294)
```rust
fn decode_i32(data: &[u8]) -> Result<i32, Error> {
    if data.len() != 4 {
        return Err(Error::Params(
            "unable to convert from bytes to i32 due to insufficient data in little-endian format"
                .to_string(),
        ));
    }
    Ok(i32::from_le_bytes(to_fixed_array(&data[0..4])))
}
```
