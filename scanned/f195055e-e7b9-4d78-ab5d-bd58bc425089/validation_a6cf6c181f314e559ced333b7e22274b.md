### Title
Unbounded Attacker-Controlled `OFFSET` in `get_tx_with_cell` SQL Query Enables Indexer DoS — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs`)

---

### Summary

An unprivileged RPC caller can craft a 12-byte `after` cursor whose last 4 bytes encode `i32::MAX` (2 147 483 647). The rich-indexer decodes this value without any bounds check and passes it verbatim as the SQL `OFFSET`, forcing the database engine to scan and discard up to 2 billion rows before returning any result. Because the rich-indexer has no query timeout (unlike the regular indexer), a single such call can monopolize the DB connection for an arbitrarily long time, starving all other indexer callers.

---

### Finding Description

**Cursor parsing — no offset validation**

`get_transactions` (ungrouped path) splits the 12-byte `after` blob into an 8-byte `last` (tx_id) and a 4-byte `offset`, then decodes both with no range check: [1](#0-0) 

`decode_i32` only checks that the slice is exactly 4 bytes; it accepts every value in `[i32::MIN, i32::MAX]`: [2](#0-1) 

**Offset injected directly into SQL**

`get_tx_with_cell` applies the decoded offset to the query builder with no cap: [3](#0-2) 

This produces SQL of the form:
```sql
SELECT ... LIMIT <limit> OFFSET 2147483647
```

**No timeout on rich-indexer SQL queries**

`AsyncRichIndexerHandle` carries only `store`, `pool`, and `request_limit` — there is no `timeout_limit` field: [4](#0-3) 

The `timeout_limit` config key exists for the regular (RocksDB) indexer and is enforced via `TimeoutIterator`, but the rich-indexer service ignores it entirely — confirmed by the absence of any `timeout_limit` reference in `util/rich-indexer/`: [5](#0-4) 

The `limit` parameter is bounded by `request_limit`, but that guard is irrelevant here because the cost is driven by the attacker-controlled `offset`, not by `limit`: [6](#0-5) 

---

### Impact Explanation

- **SQLite (default backend):** SQLite uses a single writer connection. A query with `OFFSET 2147483647` holds that connection for the full scan duration, blocking every concurrent indexer read/write.
- **PostgreSQL backend:** The query holds one connection from the pool for the full scan duration, exhausting the pool with a small number of concurrent crafted requests.
- In both cases the indexer becomes unavailable to legitimate callers for the duration of the attack, with no server-side timeout to bound the damage.

---

### Likelihood Explanation

The `after` parameter is a plain `JsonBytes` field accepted over the public JSON-RPC interface with no authentication. Any caller who can reach the RPC port (default: localhost, but commonly exposed) can send the crafted cursor. The construction is trivial: a fixed 12-byte hex string. No chain state, PoW, or privileged access is required.

---

### Recommendation

1. **Clamp the offset at parse time.** After `decode_i32`, reject or clamp any value that exceeds a small practical bound (e.g., the configured `request_limit`):
   ```rust
   if offset < 0 || offset as u32 > limit {
       return Err(Error::Params("invalid cursor offset".to_string()));
   }
   ```
2. **Add a query timeout to the rich-indexer.** Thread `timeout_limit` through `AsyncRichIndexerHandle` and wrap SQL futures with `tokio::time::timeout`, mirroring the `TimeoutIterator` pattern used in the regular indexer.
3. **Consider keyset pagination instead of OFFSET.** The `last` (tx_id) field already provides a keyset anchor; the offset is only needed to handle ties within a single tx_id. Bounding it to the maximum number of cells per transaction (a small constant) eliminates the attack surface entirely.

---

### Proof of Concept

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
        after_hex          # bytes 8-11 = 0x7FFFFFFF
    ]
})
# Send to RPC port and measure wall-clock time.
# On a node with a non-trivial indexer DB the call will hang
# far beyond any reasonable per-request budget.
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

**File:** util/rich-indexer/src/service.rs (L45-52)
```rust
        Self {
            store,
            sync,
            block_filter: config.block_filter.clone(),
            cell_filter: config.cell_filter.clone(),
            async_handle,
            request_limit: config.request_limit.unwrap_or(usize::MAX),
        }
```
