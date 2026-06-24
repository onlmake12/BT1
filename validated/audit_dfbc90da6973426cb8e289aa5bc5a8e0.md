All cited code is confirmed. Let me verify the `get_cells_capacity` behavior and check for any Security.md scope exclusions.

Audit Report

## Title
Unbounded Memory Allocation via Indexer RPC `get_cells`/`get_transactions` When `request_limit` Is Unset — (`util/indexer/src/service.rs`)

## Summary
When `IndexerConfig.request_limit` is `None` (the default), both `IndexerService::new` and `RichIndexerService::new` set the internal `request_limit` to `usize::MAX`. The limit guard in `get_cells` and `get_transactions` is trivially bypassed by any caller-supplied `limit` up to `u32::MAX`, because `u32::MAX as usize < usize::MAX` on 64-bit platforms. The only real bound on allocation is a `TimeoutIterator` (default 10 seconds), which is a time bound, not a memory bound. Within that window, all matching RocksDB records are deserialized and heap-allocated into a single `Vec`, enabling OOM-driven node crashes via repeated concurrent RPC calls.

## Finding Description

**Root cause — default config leaves `request_limit` as `None`:** [1](#0-0) [2](#0-1) 

**`IndexerService::new` converts `None` to `usize::MAX`:** [3](#0-2) 

The same applies to `RichIndexerService::new`: [4](#0-3) 

**The limit guard is trivially bypassed in `get_cells`:** [5](#0-4) 

`limit.value()` returns a `u32`, so the maximum caller-supplied value is `u32::MAX = 4_294_967_295`. Cast to `usize` on a 64-bit platform this is `4_294_967_295usize`. The guard `4_294_967_295 > usize::MAX (18_446_744_073_709_551_615)` evaluates to `false`, so the check passes unconditionally. The identical pattern exists in `get_transactions`: [6](#0-5) 

**The iterator is bounded only by a 10-second `TimeoutIterator`:** [7](#0-6) [8](#0-7) 

**All matching records within the timeout window are materialized into a `Vec`:** [9](#0-8) 

The `TimeoutIterator` stops yielding items after the wall-clock timeout, but by that point the `Vec` has already been incrementally heap-allocated. The error path at L373–374 drops the `Vec` only after peak RSS has already been reached. The same pattern exists in the ungrouped `get_transactions` path: [10](#0-9) 

**Exploit flow:**
1. Attacker sends `get_cells` with `limit = 0xFFFFFFFF` against a populated indexer store.
2. Guard check passes (`u32::MAX as usize < usize::MAX`).
3. A RocksDB snapshot iterator scans all matching records for up to 10 seconds.
4. Each `IndexerCell` (containing output, output_data, scripts, capacity, block_number, tx_index) is deserialized and pushed into a heap `Vec`.
5. On a fast NVMe-backed mainnet node with millions of UTXOs, this can allocate multiple gigabytes per request.
6. Four concurrent such requests multiply the allocation; the OS OOM-killer terminates the `ckb` process.

## Impact Explanation
**High (10001–15000 points): Vulnerabilities which could easily crash a CKB node.**

An OOM-killer invocation terminates the `ckb` process, constituting a node crash. The attack requires only a single JSON-RPC call per request and is repeatable with no cooldown. Multiple concurrent requests compound RSS growth. The node becomes unresponsive to legitimate traffic before the OOM kill, and restarts do not fix the vulnerability since the default configuration is unchanged.

## Likelihood Explanation
- The vulnerable default (`request_limit = None` → `usize::MAX`) requires no operator misconfiguration; it is the out-of-the-box state.
- Any caller with access to the RPC port (common for nodes serving dApps, wallets, or light clients) can trigger this.
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

### Citations

**File:** util/app-config/src/configs/indexer.rs (L38-40)
```rust
    /// limit of indexer request
    #[serde(default)]
    pub request_limit: Option<usize>,
```

**File:** util/app-config/src/configs/indexer.rs (L64-65)
```rust
            init_tip_hash: None,
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

**File:** util/indexer/src/service.rs (L674-675)
```rust
                .take(limit)
                .collect::<Vec<_>>();
```

**File:** util/rich-indexer/src/service.rs (L51-51)
```rust
            request_limit: config.request_limit.unwrap_or(usize::MAX),
```
