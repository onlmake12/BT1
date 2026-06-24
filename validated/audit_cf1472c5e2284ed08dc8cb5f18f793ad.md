Audit Report

## Title
Unbounded Memory Allocation via Indexer RPC `get_cells`/`get_transactions` When `request_limit` Is Unset — (`util/indexer/src/service.rs`)

## Summary
When `IndexerConfig.request_limit` is `None` (the default), both `IndexerService::new` and `RichIndexerService::new` set the internal `request_limit` to `usize::MAX`. The limit guard in `get_cells` and `get_transactions` is trivially bypassed by any caller-supplied `limit` up to `u32::MAX`, because `u32::MAX as usize < usize::MAX` on 64-bit platforms. The only real bound on allocation is a `TimeoutIterator` (default 10 seconds), which is a time bound, not a memory bound, enabling OOM-driven node crashes via repeated concurrent RPC calls.

## Finding Description

**Default config leaves `request_limit` as `None`:**
`util/app-config/src/configs/indexer.rs` L65 sets `request_limit: None` in `IndexerConfig::default()`.

**`IndexerService::new` converts `None` to `usize::MAX`:**
`util/indexer/src/service.rs` L98:
```rust
request_limit: config.request_limit.unwrap_or(usize::MAX),
```
`RichIndexerService::new` does the same at `util/rich-indexer/src/service.rs` L51.

**The limit guard is trivially bypassed in `get_cells` (`util/indexer/src/service.rs` L212–221):**
```rust
let limit = limit.value() as usize;
if limit > self.request_limit { ... }
```
`limit.value()` returns a `u32`. On a 64-bit platform, `u32::MAX as usize = 4_294_967_295`, while `usize::MAX = 18_446_744_073_709_551_615`. The condition `4_294_967_295 > 18_446_744_073_709_551_615` is always `false`, so the guard never fires. The identical pattern exists in `get_transactions` at L388–397.

**The iterator is bounded only by a 10-second `TimeoutIterator`:**
`util/indexer/src/service.rs` L242:
```rust
let mut iter = TimeoutIterator::new(snapshot.iterator(mode).skip(skip), self.timeout_limit);
```
The `TimeoutIterator::next()` (L55–61) returns `None` only after `self.start_time.elapsed() > self.timeout`.

**All matching records within the timeout window are materialized into a `Vec`:**
`util/indexer/src/service.rs` L371–372:
```rust
.take(limit)
.collect::<Vec<_>>();
```
With `limit = u32::MAX`, `.take(limit)` imposes no practical bound. The `Vec` grows incrementally for the full 10-second scan window. The error check at L373–374 drops the `Vec` only after peak RSS has already been reached. The same pattern exists in the ungrouped `get_transactions` path at L674–675.

**Exploit flow:**
1. Attacker sends `get_cells` with `limit = 0xFFFFFFFF` against a populated indexer store.
2. Guard check passes (`u32::MAX as usize < usize::MAX`).
3. A RocksDB snapshot iterator scans all matching records for up to 10 seconds.
4. Each `IndexerCell` (output, output_data, scripts, capacity, block_number, tx_index) is deserialized and pushed into a heap `Vec`.
5. On a fast NVMe-backed mainnet node with millions of UTXOs, this can allocate multiple gigabytes per request.
6. Four concurrent such requests multiply the allocation; the OS OOM-killer terminates the `ckb` process.

Note: `get_cells_capacity` is not affected by this issue — it accumulates a `u64` sum rather than a `Vec`, so its memory usage is bounded regardless of store size.

## Impact Explanation
**High (10001–15000 points): Vulnerabilities which could easily crash a CKB node.**

An OOM-killer invocation terminates the `ckb` process, constituting a node crash. The attack requires only a single JSON-RPC call per request and is repeatable with no cooldown. Multiple concurrent requests compound RSS growth. The node becomes unresponsive to legitimate traffic before the OOM kill, and restarts do not fix the vulnerability since the default configuration is unchanged.

## Likelihood Explanation
- The vulnerable default (`request_limit = None` → `usize::MAX`) requires no operator misconfiguration; it is the out-of-the-box state, confirmed by `IndexerConfig::default()`.
- Any caller with network access to the RPC port (common for nodes serving dApps, wallets, or light clients) can trigger this.
- The exploit is a single JSON-RPC call with one field set to `0xffffffff`; no authentication, no special tooling, no chain state manipulation required.
- The `TimeoutIterator` mitigates a full-store scan but does not prevent large allocations on a populated store within the 10-second window.
- The same vulnerability exists in `RichIndexerService` and `AsyncRichIndexerHandle`, doubling the attack surface.

## Recommendation
1. Change the default for `request_limit` from `None`/`usize::MAX` to a safe bounded value (e.g., `500` or `1000`) in `IndexerConfig::default()`.
2. Enforce a hard cap independent of configuration in both `get_cells` and `get_transactions`:
   ```rust
   const MAX_INDEXER_LIMIT: usize = 1000;
   let effective_limit = limit.min(self.request_limit).min(MAX_INDEXER_LIMIT);
   ```
3. Apply the same fix to `RichIndexerService` and `AsyncRichIndexerHandle`, which share the same `unwrap_or(usize::MAX)` pattern.

## Proof of Concept
```bash
# Against a node with default config and a populated indexer store:
for i in 1 2 3 4; do
  curl -s -X POST http://localhost:8114 \
    -H 'Content-Type: application/json' \
    -d '{
      "jsonrpc": "2.0",
      "method": "get_cells",
      "params": [
        {"script": {"code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
                    "hash_type": "type", "args": "0x"},
         "script_type": "lock"},
        "asc",
        "0xffffffff",
        null
      ],
      "id": 1
    }' &
done
wait
# Monitor RSS of the ckb process during the 10-second scan window with:
# watch -n0.5 'ps -o pid,rss,vsz -p $(pgrep ckb)'
# Repeat to sustain memory pressure until OOM kill.
```