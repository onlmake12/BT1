The code has been verified. All cited line numbers and code snippets match exactly.

**Verification summary:**

- Lines 44–54 of `get_transactions.rs`: cursor is validated only for length (12 bytes), then split into `last_id` (i64) and `offset` (i32) with no range check. [1](#0-0) 
- Lines 286–294 of `mod.rs`: `decode_i32` performs only a length check, no upper-bound validation. [2](#0-1) 
- Lines 179–182 of `get_transactions.rs`: the decoded offset is passed verbatim to `query_builder.offset(offset)`. [3](#0-2) 
- Lines 23–32 of `get_transactions.rs`: only `limit` is bounded by `request_limit`; `offset` has no corresponding guard. [4](#0-3) 

---

Audit Report

## Title
Unbounded Attacker-Controlled SQL OFFSET in `get_tx_with_cell` Enables DoS via Crafted Pagination Cursor — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs`)

## Summary
When `group_by_transaction` is `false` or absent, `get_transactions` decodes the last 4 bytes of the `after` cursor as an i32 SQL OFFSET with no upper-bound validation. An unprivileged caller can supply a crafted 12-byte cursor with `offset = i32::MAX` (2,147,483,647), forcing the database to scan and discard billions of rows before returning results. Repeated requests render the indexer RPC unresponsive without crashing the process.

## Finding Description
In `get_transactions.rs` lines 44–54, the `after` cursor is validated only for byte length (must be exactly 12 bytes). The first 8 bytes are decoded as `last_id` (i64) and the final 4 bytes as `offset` (i32) via `decode_i32`:

```rust
if after.len() != 12 { return Err(...); }
let (last, offset) = after.as_bytes().split_at(after.len() - 4);
let last = decode_i64(last)?;
let offset = decode_i32(offset)?;
last_cursor = Some((last, offset));
```

`decode_i32` in `mod.rs` lines 286–294 performs only a length check — no range validation is applied:

```rust
fn decode_i32(data: &[u8]) -> Result<i32, Error> {
    if data.len() != 4 { return Err(...); }
    Ok(i32::from_le_bytes(to_fixed_array(&data[0..4])))
}
```

The decoded offset is then passed verbatim to the SQL engine at lines 179–182:

```rust
query_builder.limit(limit);
if let Some((_, offset)) = last_cursor {
    query_builder.offset(offset);
}
```

The `request_limit` guard at lines 23–32 caps only the number of returned rows (`limit`), not the `offset`. SQL `OFFSET N` requires the database to materialize and discard N rows before returning results. For the complex UNION query built by `build_tx_with_cell_union_sub_query`, this means a full sequential scan of the filtered result set up to position N. With `offset = i32::MAX`, every such request forces the database to attempt to skip over 2,147,483,647 rows, saturating the database thread for the duration of the query.

## Impact Explanation
The rich-indexer RPC endpoint (`get_transactions`) becomes effectively unresponsive under repeated crafted requests. Each request with a maximally crafted offset saturates the database thread, blocking all concurrent legitimate indexer queries. This maps to **Note (0–500 points): Any local RPC API crash** — the indexer RPC API is rendered non-functional without crashing the process. The impact is confined to the indexer service and does not affect CKB node consensus, P2P networking, or block production.

## Likelihood Explanation
The rich-indexer is a documented, production-enabled feature. The RPC endpoint is unauthenticated by default. The cursor format is trivially derivable: any real `last_cursor` returned by `get_transactions` is 12 bytes, and the attacker need only replace the last 4 bytes with `\xff\xff\xff\x7f` (i32::MAX in little-endian). No special privileges, keys, or chain access are required. The exploit is repeatable at will by any caller with access to the RPC endpoint.

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
4. Submit `get_transactions` with this crafted `after` cursor repeatedly.
5. Observe that query latency grows proportionally to the offset value. The 12-byte length check at line 45 passes, `decode_i32` succeeds, and `query_builder.offset(2147483647)` is emitted into the SQL, forcing the DB to attempt to skip over two billion rows before returning results, blocking the database thread for the duration.

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
