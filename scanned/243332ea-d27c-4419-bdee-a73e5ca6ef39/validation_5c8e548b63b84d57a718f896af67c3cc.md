The code confirms this is a real, reachable vulnerability. Here is the full analysis:

---

### Title
Attacker-Controlled SQL OFFSET in `get_tx_with_cell` Enables O(n) Database Scan via Crafted Pagination Cursor — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs`)

### Summary
The ungrouped `get_transactions` RPC path in the rich-indexer decodes the `after` cursor's last 4 bytes as an i32 OFFSET value and passes it directly to the SQL engine with no upper-bound validation. An unprivileged caller can craft a cursor with `offset = i32::MAX` (≈2.1 billion), forcing the database to skip over billions of rows before returning `limit` results, causing severe and sustained query latency.

### Finding Description
When `group_by_transaction` is `false` or absent, `get_transactions` parses the `after` parameter as a 12-byte blob: [1](#0-0) 

The last 4 bytes are decoded as an i32 via `decode_i32`, which performs **only a length check** — no range validation: [2](#0-1) 

This decoded value is then passed verbatim as a SQL `OFFSET`: [3](#0-2) 

The `request_limit` guard only caps the `limit` (number of returned rows), not the `offset`: [4](#0-3) 

SQL `OFFSET N` requires the database engine to materialize and discard N rows before returning results. With `offset = i32::MAX = 2,147,483,647`, every such request forces a full sequential scan of the result set.

### Impact Explanation
Any caller with access to the rich-indexer RPC endpoint (no authentication required by default) can repeatedly submit crafted `after` cursors. Each request causes the SQLite or PostgreSQL backend to perform O(offset) work. With `offset = 2^30`, a single request can saturate the DB thread for seconds to minutes depending on dataset size. Repeated requests constitute a sustained denial-of-service against the indexer, blocking all legitimate indexer queries.

### Likelihood Explanation
The rich-indexer is a documented, production-enabled feature. The RPC endpoint is unauthenticated by default. The cursor format (12 bytes, last 4 = offset) is derivable from the `last_cursor` returned by any real `get_transactions` call, or can be crafted from scratch since the only validation is the 12-byte length check. No special privileges, keys, or hashpower are required.

### Recommendation
Add an upper-bound check on the decoded offset before using it in the query. The offset should never exceed the configured `request_limit` (or some small multiple of it), since a legitimate cursor produced by the server will always have `offset <= limit`. Reject or clamp any cursor whose offset exceeds this bound:

```rust
// After decoding:
if offset as u32 > self.request_limit as u32 {
    return Err(Error::Params("Invalid cursor: offset out of range".to_string()));
}
```

Alternatively, redesign the cursor to use a `(last_tx_id, last_io_index)` keyset pair instead of a SQL OFFSET, making pagination O(1) regardless of position.

### Proof of Concept
1. Enable rich-indexer. Index a chain with a widely-used lock script appearing in many tx inputs/outputs.
2. Call `get_transactions` with `group_by_transaction=false`, `limit=1` to obtain a valid `last_cursor` (12 bytes).
3. Craft a new `after` value: keep the first 8 bytes (any valid `last_id` LE), replace the last 4 bytes with `\x00\x00\x00\x40` (= 1,073,741,824 in LE).
4. Submit `get_transactions` with this crafted `after` cursor.
5. Observe that query latency grows proportionally to the offset value, confirming O(n) scan behavior.

The cursor length check at line 45 passes (12 bytes), `decode_i32` succeeds, and `query_builder.offset(1073741824)` is emitted into the SQL, forcing the DB to skip over one billion rows. [5](#0-4) [6](#0-5)

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
