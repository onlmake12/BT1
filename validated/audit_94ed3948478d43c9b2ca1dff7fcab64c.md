I have all the information needed. Let me trace the full call chain precisely.

### Title
Unbounded Memory Allocation via Indexer RPC `get_cells`/`get_transactions` When `request_limit` Is Unset — (`util/indexer/src/service.rs`)

---

### Summary

When `IndexerConfig.request_limit` is `None` (the default), `IndexerService::new` sets the internal `request_limit` to `usize::MAX`. The limit guard in `IndexerHandle::get_cells` and `get_transactions` then passes for any caller-supplied `limit` up to `u32::MAX`, because `u32::MAX as usize < usize::MAX` on 64-bit platforms. The only real bound on memory allocation is a `TimeoutIterator` (default 10 seconds), which is a time bound, not a memory bound. Within that window, millions of RocksDB records can be read and materialized into a heap-allocated `Vec`, causing severe memory pressure or OOM.

---

### Finding Description

**Step 1 — Default configuration leaves `request_limit` as `None`:** [1](#0-0) [2](#0-1) 

**Step 2 — `IndexerService::new` converts `None` to `usize::MAX`:** [3](#0-2) 

**Step 3 — The limit guard in `get_cells` is trivially bypassed:**

```rust
let limit = limit.value() as usize;   // u32::MAX → 4_294_967_295usize
if limit == 0 { return Err(...) }
if limit > self.request_limit { ... } // 4_294_967_295 > usize::MAX → false, passes
``` [4](#0-3) 

The same pattern is present in `get_transactions`: [5](#0-4) 

**Step 4 — The iterator is bounded only by a 10-second `TimeoutIterator`, not by memory:** [6](#0-5) 

The `TimeoutIterator` stops yielding items after the elapsed wall-clock time exceeds the timeout, but it does not cap the number of bytes allocated: [7](#0-6) 

**Step 5 — All matching records within the timeout window are materialized into a `Vec`:** [8](#0-7) 

On a fast NVMe-backed node with a large indexer store, millions of `IndexerCell` records (each containing output data, scripts, capacity, etc.) can be read and heap-allocated within 10 seconds. The same applies to `RichIndexerService`, which also defaults `request_limit` to `usize::MAX`: [9](#0-8) 

---

### Impact Explanation

An unprivileged caller with access to the RPC port can send repeated `get_cells` or `get_transactions` requests with `limit = 0xFFFFFFFF` against a populated indexer store. Each request causes the node to:

1. Open a RocksDB snapshot iterator
2. Scan and deserialize as many records as possible within 10 seconds
3. Allocate all results into a single `Vec` before returning

Multiple concurrent such requests compound the allocation. The result is sustained high RSS growth, potential OOM-killer invocation, or severe performance degradation making the node unresponsive to legitimate traffic.

---

### Likelihood Explanation

- The vulnerable default (`request_limit = None`) requires no special operator misconfiguration — it is the out-of-the-box state.
- The RPC endpoint is a standard, documented attack surface.
- The exploit requires only a single JSON-RPC call with a large `limit` field.
- The `TimeoutIterator` mitigates the absolute worst case (full store scan) but does not prevent large allocations within the 10-second window on a populated store.

---

### Recommendation

1. Change the default for `request_limit` from `None`/`usize::MAX` to a safe bounded value (e.g., `500` or `1000`), matching what the RPC documentation implies as a reasonable page size.
2. Alternatively, enforce a hard cap independent of configuration, e.g.:
   ```rust
   const MAX_INDEXER_LIMIT: usize = 1000;
   let effective_limit = limit.min(self.request_limit).min(MAX_INDEXER_LIMIT);
   ```
3. Apply the same fix to `RichIndexerService` and `AsyncRichIndexerHandle`.

---

### Proof of Concept

```bash
# Against a node with default config (no request_limit set) and a populated indexer:
curl -X POST http://localhost:8114 \
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
  }'
# Monitor RSS of the ckb process during the 10-second scan window.
# Repeat concurrently (e.g., 4 parallel requests) to amplify memory pressure.
```

### Citations

**File:** util/app-config/src/configs/indexer.rs (L39-40)
```rust
    #[serde(default)]
    pub request_limit: Option<usize>,
```

**File:** util/app-config/src/configs/indexer.rs (L65-65)
```rust
            request_limit: None,
```

**File:** util/indexer/src/service.rs (L55-61)
```rust
    fn next(&mut self) -> Option<Self::Item> {
        if self.start_time.elapsed() > self.timeout {
            self.timed_out = true;
            return None;
        }
        self.inner.next()
    }
```

**File:** util/indexer/src/service.rs (L98-99)
```rust
            request_limit: config.request_limit.unwrap_or(usize::MAX),
            timeout_limit: Duration::from_secs(config.timeout_limit.unwrap_or(10)),
```

**File:** util/indexer/src/service.rs (L212-221)
```rust
        let limit = limit.value() as usize;
        if limit == 0 {
            return Err(Error::invalid_params("limit should be greater than 0"));
        }
        if limit > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
        }
```

**File:** util/indexer/src/service.rs (L242-242)
```rust
        let mut iter = TimeoutIterator::new(snapshot.iterator(mode).skip(skip), self.timeout_limit);
```

**File:** util/indexer/src/service.rs (L371-372)
```rust
            .take(limit)
            .collect::<Vec<_>>();
```

**File:** util/indexer/src/service.rs (L388-397)
```rust
        let limit = limit.value() as usize;
        if limit == 0 {
            return Err(Error::invalid_params("limit should be greater than 0"));
        }
        if limit > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
        }
```

**File:** util/rich-indexer/src/service.rs (L51-51)
```rust
            request_limit: config.request_limit.unwrap_or(usize::MAX),
```
