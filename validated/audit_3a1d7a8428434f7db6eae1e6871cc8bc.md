Audit Report

## Title
Default `usize::MAX` Request Limit Bypasses Guard, Enabling Unbounded RocksDB Iteration and Memory Exhaustion — (File: `util/indexer/src/service.rs`)

## Summary
`IndexerService` defaults `request_limit` to `usize::MAX` when unconfigured. Because the RPC `limit` parameter is a `Uint32` (max `4_294_967_295`), and `u32::MAX < usize::MAX` on all 64-bit targets, the guard `if limit > self.request_limit` is permanently false under the default configuration. An unprivileged caller can pass `limit = 0xFFFFFFFF` to `get_cells` or `get_transactions`, causing the node to iterate RocksDB and accumulate results into a heap-allocated `Vec` for the full 10-second timeout window per request.

## Finding Description
In `util/indexer/src/service.rs` line 98, `IndexerService::new` sets:
```rust
request_limit: config.request_limit.unwrap_or(usize::MAX),
``` [1](#0-0) 

The corresponding `IndexerConfig::default()` in `util/app-config/src/configs/indexer.rs` line 65 sets `request_limit: None`, so the effective cap is `usize::MAX` (`18_446_744_073_709_551_615`) unless an operator explicitly overrides it. [2](#0-1) 

In `get_cells` (line 212), the RPC `limit: Uint32` is widened:
```rust
let limit = limit.value() as usize;
```
The guard at line 216:
```rust
if limit > self.request_limit { ... }
```
is always false because `u32::MAX` (`4_294_967_295`) is strictly less than `usize::MAX` on 64-bit. [3](#0-2) 

The identical guard in `get_transactions` at line 392 has the same flaw. [4](#0-3) 

After the guard, both methods feed the limit into `.take(limit).collect::<Vec<_>>()`, bounded only by the `TimeoutIterator` (default 10 seconds). During those 10 seconds, the node continuously reads RocksDB records and pushes deserialized structs into a heap-allocated `Vec`. [5](#0-4) 

## Impact Explanation
**High — Vulnerabilities which could easily crash a CKB node.**

Each request with `limit = 0xFFFFFFFF` occupies a thread and accumulates memory for 10 seconds. Sending 20–50 concurrent requests multiplies memory pressure proportionally. On a mainnet node with large indexed state, this can exhaust available RAM and trigger an OOM-kill of the CKB process, taking the node offline.

## Likelihood Explanation
Any unprivileged caller with network access to the RPC port can trigger this. The indexer RPC (`get_cells`, `get_transactions`) is publicly documented and requires no authentication, special role, or key material. The attacker only needs to craft a standard JSON-RPC request with `limit` set to `4294967295`. The default configuration (no explicit `request_limit`) is the common deployment scenario, making this trivially exploitable on any node with the indexer module enabled.

## Recommendation
1. **Replace `usize::MAX` with a bounded default** in `util/indexer/src/service.rs` line 98:
   ```rust
   request_limit: config.request_limit.unwrap_or(1000),
   ```
2. **Enforce a hard cap** regardless of operator configuration (e.g., `u16::MAX` or a fixed constant), so even an explicitly misconfigured node cannot be trivially exhausted.
3. **Update the default config template** to document that `request_limit` must be set to a finite value when the indexer is exposed publicly.

## Proof of Concept
Send the following JSON-RPC request to a CKB node with the indexer enabled and default configuration:
```json
{
  "id": 1,
  "jsonrpc": "2.0",
  "method": "get_cells",
  "params": [
    {
      "script": {
        "code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
        "hash_type": "type",
        "args": "0x"
      },
      "script_type": "lock"
    },
    "asc",
    "0xffffffff",
    null
  ]
}
```
With `request_limit` unset (default `usize::MAX`), the guard at line 216 of `util/indexer/src/service.rs` passes because `0xffffffff` (`4_294_967_295`) `<` `usize::MAX`. The node iterates RocksDB for 10 seconds, accumulating all matching cells into memory. Sending 20+ concurrent copies of this request multiplies memory consumption proportionally, potentially exhausting available RAM and crashing the node process.

### Citations

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

**File:** util/app-config/src/configs/indexer.rs (L65-65)
```rust
            request_limit: None,
```
