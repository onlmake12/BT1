Audit Report

## Title
Unbounded Attacker-Controlled `OFFSET` in `get_tx_with_cell` SQL Query Enables Indexer RPC DoS — (`util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs`)

## Summary
The `get_transactions` RPC endpoint (ungrouped path) decodes the last 4 bytes of the `after` cursor as an `i32` offset with no bounds check, then passes it verbatim as the SQL `OFFSET` clause. An attacker can supply `offset = i32::MAX` (2,147,483,647), forcing the database to scan up to 2 billion rows before returning results. No query timeout exists in `AsyncRichIndexerHandle`, so the query runs to completion, blocking the indexer RPC for all concurrent callers.

## Finding Description
In `get_transactions` (ungrouped path), the `after` cursor is validated only for length (12 bytes), then split and decoded:

```rust
// get_transactions.rs L44-53
if after.len() != 12 { ... }
let (last, offset) = after.as_bytes().split_at(after.len() - 4);
let last = decode_i64(last)?;
let offset = decode_i32(offset)?;
last_cursor = Some((last, offset));
```

`decode_i32` performs only a length check, accepting every value in `[i32::MIN, i32::MAX]`:

```rust
// mod.rs L286-294
fn decode_i32(data: &[u8]) -> Result<i32, Error> {
    if data.len() != 4 { return Err(...); }
    Ok(i32::from_le_bytes(to_fixed_array(&data[0..4])))
}
```

The decoded offset is applied without any cap in `get_tx_with_cell`:

```rust
// get_transactions.rs L179-182
query_builder.limit(limit);
if let Some((_, offset)) = last_cursor {
    query_builder.offset(offset);
}
```

This produces SQL of the form `SELECT ... WHERE tx_id >= 1 ORDER BY tx_id LIMIT <limit> OFFSET 2147483647`, forcing the DB engine to skip up to 2 billion rows before returning results.

The existing `request_limit` guard only bounds the `limit` parameter (L27–32) and provides no protection against a large `offset`. `AsyncRichIndexerHandle` carries only `store`, `pool`, and `request_limit` fields (mod.rs L22–27) — no `timeout_limit` and no `tokio::time::timeout` wrapping the SQL future anywhere in the rich-indexer path.

## Impact Explanation
This matches **Note (0–500 points): Any local RPC API crash**. A single crafted RPC call renders the rich-indexer's `get_transactions` endpoint unresponsive for the duration of the scan. On SQLite, this also blocks all concurrent indexer queries due to the writer lock. The core CKB node (consensus, p2p) is unaffected; the impact is confined to the indexer RPC service.

## Likelihood Explanation
The `after` parameter is a plain `JsonBytes` field on the public JSON-RPC interface with no authentication. The crafted cursor is a fixed 12-byte value requiring no chain state, PoW, or privileged access. Any caller who can reach the RPC port can trigger this. The default binding is localhost, but many operators expose the RPC port externally. The attack is trivially repeatable and can be issued concurrently to exhaust the SQLite writer lock or PostgreSQL connection pool.

## Recommendation
1. **Clamp the offset at parse time.** After `decode_i32`, reject any value outside a small practical bound:
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
# Repeating concurrently exhausts the SQLite writer lock or PostgreSQL connection pool.
```