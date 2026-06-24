Audit Report

## Title
Unbounded Cell Iteration in `get_cells_capacity` Enables RPC-Triggered CPU Exhaustion and Tx-Pool Lock Contention — (File: `util/indexer/src/service.rs`)

## Summary
`IndexerHandle::get_cells_capacity` performs an unbounded RocksDB scan over all matching live cells, protected only by a wall-clock `TimeoutIterator` (default 10 s). Unlike `get_cells` and `get_transactions`, it enforces no caller-supplied `limit` and never checks `self.request_limit`. During the entire scan it holds the tx-pool `RwLock` in read mode, blocking any writer (block processing, tx submission) that needs the write lock. An unprivileged attacker sending a small number of concurrent broad-prefix requests can saturate the RPC thread pool and stall tx-pool writers for the full timeout window.

## Finding Description

**`get_cells` enforces both a caller limit and a server-side `request_limit`:**

```rust
// util/indexer/src/service.rs  lines 212-221
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

**`get_cells_capacity` has neither check.** After the `Partial`-mode guard it goes directly to an unbounded scan:

```rust
// util/indexer/src/service.rs  lines 686-720
pub fn get_cells_capacity(
    &self,
    search_key: IndexerSearchKey,
) -> Result<Option<IndexerCellsCapacity>, Error> {
    // ... only Partial-mode guard, no limit, no request_limit check ...
    let mut iter = TimeoutIterator::new(snapshot.iterator(mode).skip(skip), self.timeout_limit);
```

The tx-pool read lock is acquired before the scan and held for its entire duration:

```rust
// util/indexer/src/service.rs  lines 721-724
let pool = self
    .pool
    .as_ref()
    .map(|pool| pool.read().expect("acquire lock"));
```

The unbounded `.take_while(...).filter_map(...).sum()` then runs until the prefix is exhausted or the timeout fires:

```rust
// util/indexer/src/service.rs  lines 726-836
let capacity: u64 = iter
    .by_ref()
    .take_while(|(key, _value)| key.starts_with(&prefix))
    .filter_map(|(key, value)| { /* per-cell RocksDB lookup + filter work */ })
    .sum();
```

The `TimeoutIterator` only caps wall-clock time; it does not cap the number of cells processed before the timeout fires:

```rust
// util/indexer/src/service.rs  lines 55-61
fn next(&mut self) -> Option<Self::Item> {
    if self.start_time.elapsed() > self.timeout {
        self.timed_out = true;
        return None;
    }
    self.inner.next()
}
```

Default configuration sets `request_limit = usize::MAX` and `timeout_limit = 10 s`:

```rust
// util/indexer/src/service.rs  lines 98-99
request_limit: config.request_limit.unwrap_or(usize::MAX),
timeout_limit: Duration::from_secs(config.timeout_limit.unwrap_or(10)),
```

The RPC entry point accepts only a `search_key` with no `limit` parameter and applies no authentication or rate-limiting before reaching `IndexerHandle::get_cells_capacity`:

```rust
// rpc/src/module/indexer.rs  lines 879-883
#[rpc(name = "get_cells_capacity")]
fn get_cells_capacity(
    &self,
    search_key: IndexerSearchKey,
) -> Result<Option<IndexerCellsCapacity>>;
```

**Exploit flow:**
1. Attacker sends 10–20 concurrent `get_cells_capacity` requests with `code_hash` = secp256k1 lock and `args = "0x"` (prefix mode, matches all secp256k1 cells — tens of millions on mainnet).
2. Each request enters `IndexerHandle::get_cells_capacity`, acquires the tx-pool read lock, and begins iterating all matching cells.
3. Each request consumes up to 10 s of CPU and holds the tx-pool read lock for the same duration.
4. Any concurrent tx-pool writer (block processing notification, new tx submission) that needs the write lock is blocked until all readers release — up to 10 s.
5. The RPC thread pool is saturated; legitimate RPC callers queue or time out.
6. Requests can be repeated indefinitely with no on-chain cost.

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

The attack requires only standard JSON-RPC HTTP requests — no keys, no funds, no special role. A small number of concurrent requests (10–20) can:
- Saturate the node's RPC thread pool for the full 10 s timeout window, blocking all other RPC callers.
- Hold the tx-pool `RwLock` in read mode for up to 10 s per request, blocking tx-pool writers. This delays block processing notifications and new transaction submissions on the targeted node.

Infrastructure nodes (wallets, explorers, dApps) that enable `--indexer` are the primary targets. Degrading these nodes causes effective network congestion for their users at negligible attacker cost, satisfying the "few costs" criterion.

## Likelihood Explanation

- The indexer RPC is publicly accessible on any node that enables `--indexer`, which is standard for infrastructure nodes.
- The secp256k1 lock `code_hash` is publicly known and has tens of millions of live cells on CKB mainnet.
- The attack requires only a standard HTTP client; no privileged access, no on-chain funds.
- The default `timeout_limit` of 10 s and `request_limit` of `usize::MAX` are the out-of-the-box values; operators are unlikely to have tightened them.
- The attack is repeatable indefinitely with no cooldown or cost.

## Recommendation

1. **Add a server-side cell-count limit to `get_cells_capacity`**: after scanning `self.request_limit` matching cells, stop iteration and return an error (mirroring the guard in `get_cells`).
2. **Release the tx-pool read lock before the scan**, or restructure so the lock is not held across the full RocksDB iteration. The pool check per cell (`is_consumed_by_pool_tx`) can be done with a short-lived lock acquisition per cell, or by snapshotting the consumed set before the scan.
3. **Consider a hard per-request cell-count cap** independent of wall-clock time, so that fast hardware cannot be forced to scan more cells than the operator intends within the timeout window.
4. **Consider rate-limiting** the `get_cells_capacity` endpoint at the RPC layer, or requiring callers to supply a narrower (exact-match) `search_key` when the result set is expected to be large.

## Proof of Concept

Send the following JSON-RPC request to a CKB node with the indexer enabled, repeated 10–20 times concurrently:

```json
{
  "id": 1,
  "jsonrpc": "2.0",
  "method": "get_cells_capacity",
  "params": [
    {
      "script": {
        "code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
        "hash_type": "type",
        "args": "0x"
      },
      "script_type": "lock"
    }
  ]
}
```

**Expected observable effects:**
- Each request runs for ~10 s (until `TimeoutIterator` fires) and returns `Error: Indexer request timeout`.
- During those 10 s, the tx-pool read lock is held; any concurrent tx-pool write operation (e.g., `send_transaction`) blocks until the lock is released.
- Legitimate RPC calls queue behind the saturated thread pool.

**Minimal unit test plan:**
1. Populate a test indexer store with a large number of cells sharing the same lock script prefix.
2. Spawn 10 concurrent threads each calling `IndexerHandle::get_cells_capacity` with that prefix.
3. Concurrently attempt a tx-pool write operation and measure its latency — it should be blocked for up to `timeout_limit` seconds.
4. Verify that `get_cells` with the same prefix and `limit = 1` returns immediately, confirming the asymmetry.