Audit Report

## Title
Unbounded `OFFSET` via Crafted Pagination Cursor in `get_transactions` (Ungrouped) — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs`)

## Summary
The `get_transactions` RPC endpoint accepts a 12-byte `after` cursor whose last 4 bytes are decoded as a signed `i32` and passed verbatim as `OFFSET` to the underlying SQL query with no upper-bound check. An unprivileged caller can supply `offset = i32::MAX` (2,147,483,647), forcing the database to scan and discard up to 2 billion rows before returning an empty result set. Repeated concurrent requests exhaust the `sqlx` connection pool, rendering the indexer RPC unavailable.

## Finding Description
**Exact call chain:**

1. **Limit guard** (`get_transactions.rs` L23–32) only bounds the number of *returned* rows; it places no constraint on the `OFFSET` value. [1](#0-0) 

2. **Cursor decoding** (`get_transactions.rs` L44–53): the last 4 bytes of `after` are decoded via `decode_i32` with no upper-bound check on the resulting value. [2](#0-1) 

3. **Offset applied** (`get_transactions.rs` L180–182): the decoded value is passed directly to `query_builder.offset(offset)`. [3](#0-2) 

4. **`decode_i32`** (`mod.rs` L286–294) performs only a length check; no value-range validation exists. [4](#0-3) 

The generated SQL becomes `… LIMIT <limit> OFFSET 2147483647`, forcing a full sequential scan. Setting `last = 0` (first 8 bytes) maximises the rows the DB must traverse before discarding them.

## Impact Explanation
This maps to **Note (0–500 points): Any local RPC API crash**. The rich-indexer RPC endpoint becomes unavailable under repeated crafted requests: on PostgreSQL, each long-running query holds a connection from the `sqlx` pool; concurrent crafted requests exhaust the pool, causing all subsequent RPC calls to queue indefinitely or fail. On SQLite, the single-writer read lock serialises all concurrent DB operations. The core CKB node (consensus, P2P, block production) is unaffected; the impact is confined to the indexer RPC service on the targeted node.

## Likelihood Explanation
The `after` cursor is a documented public pagination parameter. Any caller with network access to the RPC port (default 8116) can craft the 12-byte value with no authentication, proof-of-work, or privileged role. Indexer nodes are routinely exposed publicly. The attack is trivially repeatable in a tight loop.

## Recommendation
Add an upper-bound check on the decoded offset immediately after `decode_i32`, capping it at a reasonable maximum (e.g., tied to `self.request_limit`):

```rust
let offset = decode_i32(offset)?;
if offset < 0 || offset as usize > self.request_limit {
    return Err(Error::Params(format!(
        "cursor offset must be between 0 and {}",
        self.request_limit
    )));
}
```

Alternatively, redesign the cursor to use a keyset-only scheme (store only the last `tx_id` plus a tie-breaker) so that `OFFSET` is never needed and pagination is O(log N) regardless of cursor content.

## Proof of Concept
```python
import json, socket, struct

# Craft after cursor: last=0 (8 bytes LE), offset=i32::MAX (4 bytes LE)
last   = struct.pack('<q', 0)
offset = struct.pack('<i', 2147483647)
after_hex = "0x" + (last + offset).hex()

payload = json.dumps({
    "jsonrpc": "2.0", "id": 1,
    "method": "get_transactions",
    "params": [
        {"script": {"code_hash": "0x" + "00"*32, "hash_type": "data", "args": "0x"},
         "script_type": "lock"},
        "asc", "0x1",
        after_hex
    ]
})
# Send to RPC port 8116; measure response time.
# Repeat concurrently to exhaust the sqlx connection pool.
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
