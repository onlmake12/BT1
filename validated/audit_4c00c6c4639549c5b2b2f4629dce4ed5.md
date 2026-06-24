Audit Report

## Title
Unbounded RocksDB Iteration in `get_cells_capacity` Enables RPC-Triggered DoS — (`util/indexer/src/service.rs`)

## Summary
`IndexerHandle::get_cells_capacity` iterates over all matching RocksDB cells with no `request_limit` guard and no `.take(N)` early-stop, unlike `get_cells` and `get_transactions` which enforce both. The sole protection is a `TimeoutIterator` defaulting to 10 seconds. An unprivileged caller can send concurrent broad-prefix requests, each holding a worker thread for the full timeout duration, saturating the RPC thread pool and making the node unresponsive.

## Finding Description
In `util/indexer/src/service.rs`, `get_cells_capacity` (line 686) builds a RocksDB prefix iterator and folds over every matching cell with no count-based limit:

```rust
// Line 720 — only protection is a timeout wrapper
let mut iter = TimeoutIterator::new(snapshot.iterator(mode).skip(skip), self.timeout_limit);

let capacity: u64 = iter
    .by_ref()
    .take_while(|(key, _value)| key.starts_with(&prefix))
    .filter_map(|(key, value)| { ... })
    .sum();
```

By contrast, `get_cells` (lines 212–221) and `get_transactions` (lines 388–397) both enforce:
```rust
if limit > self.request_limit {
    return Err(Error::invalid_params(...));
}
```
and terminate early with `.take(limit)`.

The `request_limit` field defaults to `usize::MAX` (line 98) when unconfigured, and `timeout_limit` defaults to 10 seconds (line 99). The `TimeoutIterator` checks elapsed time only at the start of each `next()` call (lines 55–60), meaning a single expensive RocksDB read inside the loop body can still exceed the timeout boundary non-preemptively.

The RPC trait exposes `get_cells_capacity` with no `limit` parameter (lines 879–883), and the implementation passes directly to `IndexerHandle::get_cells_capacity` with no additional guard (lines 929–936).

## Impact Explanation
Matches **High: Vulnerabilities which could easily crash a CKB node**. Concurrent `get_cells_capacity` requests with a broad script prefix (e.g., empty `args`) force the node to scan every live cell in the indexer store per request, each occupying a worker thread for up to 10 seconds. With 20–50 concurrent requests, the thread pool is saturated, blocking all subsequent RPC calls including `send_transaction` and `get_block_template`, rendering the node functionally unresponsive. On a mainnet node with millions of live cells, a single request already performs millions of RocksDB key reads and per-cell deserializations before timeout fires.

## Likelihood Explanation
`get_cells_capacity` is part of the standard CKB indexer API, publicly documented, and reachable by any process that can connect to the RPC port. No authentication, signature, or privileged key is required. The attack requires only repeated JSON-RPC POST requests. The default configuration (`request_limit = usize::MAX`, `timeout_limit = 10s`) maximizes exposure. Any node with the RPC port reachable from a local network or the internet is directly vulnerable.

## Recommendation
1. Add a `request_limit` check to `get_cells_capacity` identical to the one in `get_cells` and `get_transactions`, rejecting requests that would scan more than the configured limit.
2. Alternatively, add an internal `.take(self.request_limit)` to the iterator chain so the scan stops after at most `request_limit` cells.
3. Lower the default `timeout_limit` and document that `request_limit` should be configured for any publicly reachable node.

## Proof of Concept
Send N concurrent HTTP POST requests to the CKB RPC port:
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
    "script_type": "lock"
  }]
}
```
The empty `args` prefix matches every cell whose lock script uses this `code_hash`. Send 20–50 concurrent requests; observe that subsequent `get_block_template` or `send_transaction` RPC calls queue indefinitely until the timeout window expires for each batch.