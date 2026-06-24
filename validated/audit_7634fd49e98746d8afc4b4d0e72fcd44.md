Audit Report

## Title
Unbounded Attacker-Controlled `OFFSET` in `get_tx_with_cell` SQL Query Enables Indexer RPC DoS — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs`)

## Summary
The rich-indexer's `get_transactions` RPC endpoint accepts a 12-byte `after` cursor whose last 4 bytes are decoded as an `i32` offset with no bounds check. This value is passed verbatim as the SQL `OFFSET` clause, allowing any caller to force a full-table scan of up to 2 billion rows. Because `AsyncRichIndexerHandle` carries no `timeout_limit` field and no query timeout is enforced, a single crafted request can hold the database connection indefinitely, making the indexer RPC unresponsive to all other callers.

## Finding Description
In `get_transactions` (ungrouped path), the `after` cursor is split and decoded at lines 50–53:

```rust
let (last, offset) = after.as_bytes().split_at(after.len() - 4);
let last = decode_i64(last)?;
let offset = decode_i32(offset)?;
last_cursor = Some((last, offset));
``` [1](#0-0) 

`decode_i32` performs only a length check, accepting every value in `[i32::MIN, i32::MAX]`: [2](#0-1) 

The decoded offset is then applied to the query builder without any cap in `get_tx_with_cell`: [3](#0-2) 

This produces SQL of the form `SELECT ... LIMIT <limit> OFFSET 2147483647`, forcing the DB engine to skip 2 billion rows before returning results.

The existing `request_limit` guard only bounds the `limit` parameter: [4](#0-3) 

It provides no protection against a large `offset`, since the scan cost is driven entirely by the offset value.

`AsyncRichIndexerHandle` has no `timeout_limit` field — only `store`, `pool`, and `request_limit`: [5](#0-4) 

There is no `tokio::time::timeout` or equivalent wrapping the SQL future anywhere in the rich-indexer path, so the query runs to completion regardless of duration.

## Impact Explanation
This matches **Note (0–500 points): Any local RPC API crash**. A single crafted RPC call renders the rich-indexer's `get_transactions` endpoint (and, on SQLite, all concurrent indexer queries) unresponsive for the duration of the scan. The core CKB node (consensus, p2p) is unaffected; the impact is confined to the indexer RPC service. The effect is a functional hang rather than a process crash, but the availability impact on the RPC API is equivalent.

## Likelihood Explanation
The `after` parameter is a plain `JsonBytes` field on the public JSON-RPC interface with no authentication. The crafted cursor is a fixed 12-byte value requiring no chain state, PoW, or privileged access. Any caller who can reach the RPC port can trigger this. While the default binding is localhost, many operators expose the RPC port externally. The attack is trivially repeatable.

## Recommendation
1. **Clamp the offset at parse time.** After `decode_i32`, reject any value outside a small practical bound (e.g., `[0, request_limit]`):
   ```rust
   if offset < 0 || offset as usize > self.request_limit {
       return Err(Error::Params("invalid cursor offset".to_string()));
   }
   ```
2. **Add a query timeout.** Thread a `timeout_limit` through `AsyncRichIndexerHandle` and wrap SQL futures with `tokio::time::timeout`, mirroring the `TimeoutIterator` pattern used in the regular RocksDB indexer.
3. **Consider keyset pagination.** The `last` (tx_id) field already provides a keyset anchor; bounding the offset to the maximum number of cells per transaction (a small constant) eliminates the attack surface entirely.

## Proof of Concept
```python
import json, socket, struct

# Craft after = tx_id=1 (LE i64) || offset=i32::MAX (LE i32)
after_bytes = struct.pack("<q", 1) + struct.pack("<i", 2147483647)
after_hex = "0x" + after_bytes.hex()

payload = json.dumps({
    "jsonrpc": "2.0", "id": 1,
    "method": "get_transactions",
    "params": [
        {"script": {"code_hash": "0x" + "00"*32,
                    "hash_type": "data", "args": "0x"},
         "script_type": "lock"},
        "asc", "0x1",
        after_hex   # bytes 8-11 = 0x7FFFFFFF
    ]
})
# Send to RPC port; on a node with a non-trivial indexer DB the call
# will block far beyond any reasonable per-request budget.
```
The call can be verified to hang by measuring wall-clock time against a node with a populated indexer database. Repeating the call concurrently exhausts the SQLite writer lock or the PostgreSQL connection pool.

### Citations

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L27-32)
```rust
        if limit as usize > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
        }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L50-53)
```rust
                    let (last, offset) = after.as_bytes().split_at(after.len() - 4);
                    let last = decode_i64(last)?;
                    let offset = decode_i32(offset)?;
                    last_cursor = Some((last, offset));
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L179-182)
```rust
    query_builder.limit(limit);
    if let Some((_, offset)) = last_cursor {
        query_builder.offset(offset);
    }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L22-37)
```rust
#[derive(Clone)]
pub struct AsyncRichIndexerHandle {
    store: SQLXPool,
    pool: Option<Arc<RwLock<Pool>>>,
    request_limit: usize,
}

impl AsyncRichIndexerHandle {
    /// Construct new AsyncRichIndexerHandle instance
    pub fn new(store: SQLXPool, pool: Option<Arc<RwLock<Pool>>>, request_limit: usize) -> Self {
        Self {
            store,
            pool,
            request_limit,
        }
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
