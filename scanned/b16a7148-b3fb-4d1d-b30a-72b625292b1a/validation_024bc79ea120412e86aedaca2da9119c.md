Now I have all the information needed to make a determination. Let me trace the exact code path:

The code path is fully confirmed. Here is the analysis:

---

### Title
Unbounded `OFFSET` Injection via Crafted Pagination Cursor in `get_transactions` (Ungrouped) — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs`)

### Summary

An unprivileged RPC caller can supply a crafted 12-byte `after` cursor whose last 4 bytes encode `i32::MAX` (2147483647). The value is decoded without any bounds check and passed verbatim as `OFFSET 2147483647` to the underlying SQL query. Both SQLite and PostgreSQL must scan and discard up to 2 billion rows before returning an empty result set, causing severe DB performance degradation and potential RPC unavailability.

### Finding Description

**Exact call chain:**

1. **RPC entry** — `AsyncRichIndexerHandle::get_transactions` accepts `after: Option<JsonBytes>` with no semantic validation beyond a length check. [1](#0-0) 

2. **Cursor decoding** — The last 4 bytes are decoded as a signed `i32` via `decode_i32`. No upper-bound check is performed on the resulting value. [2](#0-1) 

3. **Offset applied to query builder** — The decoded `offset` is passed directly to `query_builder.offset(offset)` with no clamping or validation. [3](#0-2) 

The generated SQL becomes:
```sql
SELECT tx_id, ... FROM (...) AS res
JOIN ckb_transaction ... JOIN block ...
WHERE tx_id >= <attacker_last>
ORDER BY tx_id ASC
LIMIT <limit> OFFSET 2147483647
```

The attacker also controls `last` (first 8 bytes of the cursor), so setting it to `0` maximises the number of rows the DB must scan before discarding them.

Note that the `limit` guard (lines 24–32) only bounds the number of *returned* rows; it does not constrain the offset scan. [4](#0-3) 

### Impact Explanation

- **SQLite**: single-writer model; a long-running `OFFSET` scan holds a read lock and serialises all concurrent DB operations, stalling indexer writes and other RPC queries.
- **PostgreSQL**: each such query consumes a connection and CPU for the full scan duration. Repeated concurrent requests exhaust the `sqlx` connection pool, causing new RPC requests to queue indefinitely or fail.
- On a mainnet node with millions of indexed transactions, each crafted request forces a full sequential scan of all matching rows, which can take seconds to minutes per call.

### Likelihood Explanation

The `after` cursor is documented as a public pagination parameter. Any caller with network access to the RPC port can craft the 12-byte value. No authentication, PoW, or privileged role is required. Indexer nodes are routinely exposed publicly. The attack is trivially repeatable in a tight loop.

### Recommendation

Add an upper-bound check on the decoded `offset` immediately after `decode_i32`:

```rust
let offset = decode_i32(offset)?;
if offset < 0 {
    return Err(Error::Params("cursor offset must be non-negative".to_string()));
}
// Add a reasonable cap, e.g. tied to request_limit
if offset as usize > self.request_limit {
    return Err(Error::Params(format!(
        "cursor offset must not exceed {}",
        self.request_limit
    )));
}
```

Alternatively, redesign the cursor to use a keyset-only scheme (store only the last `tx_id` and `io_index`/`io_type` as a tie-breaker) so that `OFFSET` is never needed and the pagination is O(log N) regardless of cursor content.

### Proof of Concept

```python
import json, socket, struct

# Craft after cursor: last=0 (8 bytes LE), offset=i32::MAX (4 bytes LE)
last   = struct.pack('<q', 0)          # 8 bytes
offset = struct.pack('<i', 2147483647) # 4 bytes = [0xFF,0xFF,0xFF,0x7F]
after_hex = "0x" + (last + offset).hex()

payload = json.dumps({
    "jsonrpc": "2.0", "id": 1,
    "method": "get_transactions",
    "params": [
        {"script": {"code_hash": "0x" + "00"*32, "hash_type": "data", "args": "0x"},
         "script_type": "lock"},
        "asc", "0x1",
        after_hex   # crafted cursor
    ]
})

# Send to node RPC port (default 8116) and measure response time
# Expected: response takes seconds/minutes; repeated calls exhaust DB threads
```

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
