Audit Report

## Title
Unbounded OFFSET in `get_tx_with_cell` Allows Crafted Cursor to Trigger Full DB Scan — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs`)

## Summary
The `get_transactions` RPC (ungrouped mode) decodes the last 4 bytes of the `after` cursor into an `i32` offset and passes it directly to `query_builder.offset()` with no upper-bound validation. An unprivileged caller can supply a cursor encoding `i32::MAX` (2,147,483,647), causing the database to execute `OFFSET 2147483647` and perform a full sequential scan of the entire result set, making the indexer RPC unresponsive.

## Finding Description
`decode_i32` in `mod.rs` validates only that the input is exactly 4 bytes; it performs no range check on the decoded value. [1](#0-0) 

In `get_transactions`, the `after` cursor is accepted if and only if its total length is 12 bytes. The 4-byte suffix is decoded and stored as `last_cursor` with no maximum enforced on the offset component. [2](#0-1) 

The decoded offset is forwarded verbatim to `query_builder.offset()`, the only call site for `.offset()` in the entire rich-indexer codebase. [3](#0-2) 

By contrast, the `limit` parameter is validated against `self.request_limit` before any query is built; no equivalent guard exists for the cursor offset. [4](#0-3) 

## Impact Explanation
The impact is confined to the rich-indexer RPC subsystem. The core CKB node (consensus engine, P2P layer, block propagation) runs independently and is unaffected. The practical result is that the indexer RPC becomes unresponsive for the duration of the scan, which is bounded by the actual number of rows in the database — not by the OFFSET value itself. This matches the allowed bounty impact: **Note (0–500 points) — local RPC API crash/unresponsiveness**. The submitted claim of "High — crash a CKB node" is not supported by the evidence; the core node process continues to function normally.

## Likelihood Explanation
The rich-indexer RPC is exposed on the standard CKB RPC port with no authentication. Any network-reachable client can call `get_transactions` with a crafted `after` value. The 12-byte cursor format is derivable from any prior `get_transactions` response. The attack is deterministic and repeatable, requiring no special privileges or victim interaction.

## Recommendation
Add an explicit upper-bound check on the decoded offset immediately after `decode_i32`, before constructing `last_cursor`. The bound should not exceed `self.request_limit`, since the offset only ever needs to represent the number of rows sharing the same `tx_id` within a single page:

```rust
let offset = decode_i32(offset)?;
if offset < 0 || offset as usize > self.request_limit {
    return Err(Error::Params("cursor offset out of range".to_string()));
}
```

## Proof of Concept
```python
import struct

# 8-byte tx_id (any value) + 4-byte offset = i32::MAX in little-endian
tx_id  = b'\x00' * 8
offset = struct.pack('<i', 2147483647)   # b'\xff\xff\xff\x7f'
after  = (tx_id + offset).hex()          # "0000000000000000ffffff7f"

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
    }, "asc", "0x1", "0x" + after]
}
# Send to node RPC port (default 8114).
# Measure response time vs. a normal call — the crafted call will hold the
# DB connection for the full duration of the result-set scan.
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L180-182)
```rust
    if let Some((_, offset)) = last_cursor {
        query_builder.offset(offset);
    }
```
