All five structural facts check out against the actual source:

- `get_cells()` `request_limit` guard: confirmed [1](#0-0) 
- `get_transactions()` `request_limit` guard: confirmed [2](#0-1) 
- `get_cells_capacity()` has no `request_limit` check, only `TimeoutIterator`: confirmed [3](#0-2) 
- `TimeoutIterator` fires after `timeout_limit` and returns error: confirmed [4](#0-3) 
- Defaults `usize::MAX` / 10 s: confirmed [5](#0-4) 

---

Audit Report

## Title
Unbounded Scan in `get_cells_capacity` Enables Indexer RPC Exhaustion — (`util/indexer/src/service.rs`)

## Summary
`IndexerHandle::get_cells_capacity()` performs an unbounded RocksDB prefix scan bounded only by a wall-clock `TimeoutIterator` (default 10 s), while `get_cells()` and `get_transactions()` additionally enforce a `request_limit` count cap. An unprivileged caller can issue concurrent broad-prefix `get_cells_capacity` requests that each saturate the indexer thread for the full timeout window, degrading indexer RPC availability on the targeted node.

## Finding Description
`get_cells()` (lines 216–221) and `get_transactions()` (lines 392–397) both reject requests where the caller-supplied `limit` exceeds `self.request_limit`. `get_cells_capacity()` (lines 686–854) accepts no `limit` parameter and performs no equivalent guard. It opens a RocksDB iterator and consumes every key matching the caller-supplied prefix until either the prefix no longer matches or `TimeoutIterator` fires:

```rust
// util/indexer/src/service.rs line 720
let mut iter = TimeoutIterator::new(snapshot.iterator(mode).skip(skip), self.timeout_limit);

// lines 726–836
let capacity: u64 = iter
    .by_ref()
    .take_while(|(key, _value)| key.starts_with(&prefix))
    .filter_map(|(key, value)| { … })
    .sum();
```

With `args: "0x"` (empty) and `script_search_mode: prefix`, the prefix matches every cell locked by the given `code_hash`, forcing a full scan of potentially millions of RocksDB entries for up to `timeout_limit` seconds (default 10 s). `request_limit` defaults to `usize::MAX` and provides no protection here. N concurrent requests each occupy the indexer for 10 s of CPU/IO, starving legitimate callers.

## Impact Explanation
Impact is limited to the indexer RPC service on the targeted node. The CKB node process (consensus, p2p, block sync) is unaffected. The indexer RPC becomes slow or unresponsive for the duration of the attack. This matches **Note (0–500 points): Any local RPC API crash**.

## Likelihood Explanation
The RPC endpoint is publicly documented and requires no credentials. The attack requires only a known popular `code_hash` (trivially obtained from the chain) and the ability to send HTTP JSON-RPC requests. The 10-second timeout per request and absence of per-IP or per-method rate limiting make sustained flooding straightforward for any external caller.

## Recommendation
Add a configurable maximum-scan-count to `get_cells_capacity` that aborts and returns an error when the number of scanned entries exceeds a threshold (analogous to `request_limit`). Additionally, consider per-IP or per-method rate limiting at the RPC layer to bound concurrent request volume.

## Proof of Concept
Send N concurrent requests:
```json
{
  "id": 1,
  "jsonrpc": "2.0",
  "method": "get_cells_capacity",
  "params": [{
    "script": {
      "code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
      "hash_type": "type",
      "args": "0x"
    },
    "script_type": "lock",
    "script_search_mode": "prefix"
  }]
}
```
Each request scans all cells matching the `code_hash` prefix for up to 10 s. With N concurrent requests the indexer is continuously saturated and legitimate indexer RPC calls time out or are queued indefinitely.

### Citations

**File:** util/indexer/src/service.rs (L98-99)
```rust
            request_limit: config.request_limit.unwrap_or(usize::MAX),
            timeout_limit: Duration::from_secs(config.timeout_limit.unwrap_or(10)),
```

**File:** util/indexer/src/service.rs (L216-221)
```rust
        if limit > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
        }
```

**File:** util/indexer/src/service.rs (L392-397)
```rust
        if limit > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
        }
```

**File:** util/indexer/src/service.rs (L720-720)
```rust
        let mut iter = TimeoutIterator::new(snapshot.iterator(mode).skip(skip), self.timeout_limit);
```

**File:** util/indexer/src/service.rs (L838-839)
```rust
        if iter.is_timed_out() {
            Err(Error::invalid_params("Indexer request timeout"))
```
