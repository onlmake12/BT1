The code path is concrete and fully traceable. Here is the analysis:

---

### Title
Unbounded cursor OFFSET in `get_tx_with_cell` enables RPC-triggered indexer DoS — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs`)

### Summary

An unprivileged caller of the `get_transactions` RPC (ungrouped mode) can supply a crafted 12-byte `after` cursor whose final 4 bytes encode `i32::MAX` (0x7FFFFFFF LE). The decoded value is passed directly as a SQL `OFFSET` with no bounds check, forcing the database to scan and discard up to 2,147,483,647 rows before returning results. Repeated calls exhaust DB CPU/IO and hang the indexer service.

### Finding Description

**Cursor parsing** — `get_transactions` accepts a 12-byte `after` blob, splits it at byte 8, and decodes the trailing 4 bytes with `decode_i32`: [1](#0-0) 

`decode_i32` only validates that the slice is exactly 4 bytes; it performs no range check on the resulting value: [2](#0-1) 

**Unbounded OFFSET injection** — the decoded `offset` is forwarded verbatim to `SqlBuilder::offset()` inside `get_tx_with_cell`: [3](#0-2) 

There is no clamp, cap, or rejection between `decode_i32` and `query_builder.offset(offset)`. The `limit` is bounded by `self.request_limit` (lines 27–32), but the OFFSET is not. [4](#0-3) 

### Impact Explanation

Both SQLite and PostgreSQL implement `OFFSET N` by scanning and discarding N rows from the result set. With `OFFSET 2147483647`, the engine must traverse the entire matching result set (potentially millions of rows) before returning anything. A single request with a broad `search_key` (e.g., empty args prefix) and `OFFSET i32::MAX` can saturate the DB thread for seconds to minutes. Concurrent such requests pile up, exhausting the connection pool and making the indexer unresponsive to all callers.

### Likelihood Explanation

The attack requires only:
1. Rich-indexer enabled (opt-in but common for dApp infrastructure nodes).
2. Network access to the RPC port.
3. A single crafted 12-byte hex string: `[any 8 bytes LE] ++ [FF FF FF 7F]`.

No authentication, no key material, no privileged role. The cursor format is documented and the length check (exactly 12 bytes) is trivially satisfied.

### Recommendation

Bound the decoded offset before use. The legitimate maximum offset is the number of cells in a single transaction (bounded by block size, well under 65,536). A simple guard suffices:

```rust
let offset = decode_i32(offset)?;
if offset < 0 || offset > MAX_CELLS_PER_TX {
    return Err(Error::Params("cursor offset out of range".to_string()));
}
```

Alternatively, re-derive the offset server-side from the `last_id` rather than trusting the client-supplied value.

### Proof of Concept

```
after = 0x0100000000000000 FFFFFF7F
         ^^^^^^^^^^^^^^^^  ^^^^^^^^
         last_id = 1 (LE)  offset = i32::MAX (LE)
```

Call:
```json
{
  "method": "get_transactions",
  "params": [
    {"script": {"code_hash": "0x...", "hash_type": "type", "args": "0x"},
     "script_type": "lock"},
    "asc", "0x1",
    "0x0100000000000000ffffff7f"
  ]
}
```

Generated SQL (simplified):
```sql
SELECT tx_id, ... FROM (...) AS res
JOIN ckb_transaction ON ...
JOIN block ON ...
WHERE tx_id >= 1
ORDER BY tx_id ASC
LIMIT 1
OFFSET 2147483647
```

The DB performs a full sequential scan of all rows with `tx_id >= 1` before returning the empty result. Repeating this in a loop DoS-es the indexer.

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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L44-53)
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
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L180-182)
```rust
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
