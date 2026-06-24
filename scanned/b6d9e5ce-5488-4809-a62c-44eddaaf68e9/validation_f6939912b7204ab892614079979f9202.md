Audit Report

## Title
Unbounded Iteration Over All Tx-Pool Entries in `get_raw_tx_pool` RPC Causes Denial-of-Service Against Pool Service Loop — (File: `tx-pool/src/pool.rs`, `rpc/src/module/pool.rs`)

## Summary
The `get_raw_tx_pool` RPC endpoint dispatches to `get_all_entry_info()` or `get_all_ids()`, both of which perform full, unbounded scans of the transaction pool with no pagination, result cap, or rate limiting. Because the tx-pool service processes requests sequentially, a caller who repeatedly invokes this endpoint while the pool is filled with minimum-size transactions can saturate the service loop, blocking `send_transaction` and `get_block_template` for all other users of the node.

## Finding Description
All four cited code paths are confirmed exactly as described.

`get_raw_tx_pool` in `rpc/src/module/pool.rs` (L703–718) dispatches unconditionally to either `get_all_entry_info()` (verbose=true) or `get_all_ids()` (verbose=false) with no guard on pool size, no pagination parameter, and no rate limit.

`get_all_entry_info()` in `tx-pool/src/pool.rs` (L464–487) performs three separate unbounded collections:
- `score_sorted_iter_by_statuses(vec![Pending, Gap])` — full score-index scan filtered by status
- `sorted_proposed_iter()` — full proposed-index scan
- `conflicts_cache.iter()` — full conflict-cache scan

`score_sorted_iter_by_statuses` in `tx-pool/src/component/pool_map.rs` (L408–416) iterates the entire multi-index map via `iter_by_score().rev().filter_map(...)` with no early exit.

`remove_expired()` in `tx-pool/src/pool.rs` (L271–288) calls `self.pool_map.iter()` over every entry on every block to find expired transactions, an O(n) scan triggered automatically by reorg processing regardless of any RPC call.

No rate limiting, pagination, or per-call entry cap exists anywhere in the RPC layer for this endpoint (confirmed: no matches for `rate_limit`, `throttle`, or `RateLimit` in `rpc/src/**`).

The tx-pool service loop processes messages sequentially. Each `get_all_entry_info()` call holds the service loop busy for the full duration of the scan plus JSON serialization, queuing all concurrent pool operations behind it.

## Impact Explanation
The `max_tx_pool_size` is a byte-based limit. With minimum-size transactions (~100–200 bytes each), the pool can hold tens of thousands of entries. Repeated `get_raw_tx_pool(verbose=true)` calls while the pool is full:
- Saturate the pool service loop, causing `send_transaction` and `get_block_template` RPCs to queue and time out
- Block block assembly on mining nodes, directly impeding block production

Blocking `get_block_template` on mining nodes constitutes a practical contribution to network congestion. This matches the allowed impact: **High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation
The attack requires two roles: a tx-pool submitter (submits many small valid transactions to fill the pool) and an RPC caller (repeatedly calls `get_raw_tx_pool`). Neither requires elevated privilege. The RPC port is localhost-only by default, but public RPC nodes (mining pools, explorers, public endpoints) are directly externally exploitable. On localhost-only nodes, any co-located process or compromised dependency can trigger this. The `remove_expired` O(n) scan compounds the effect automatically on every new block with no attacker action required beyond filling the pool.

## Recommendation
1. Add a `limit` and `after` (cursor) pagination parameter to `get_raw_tx_pool`; enforce a hard per-call cap (e.g., 1,000 entries) inside `get_all_entry_info` and `get_all_ids`.
2. Maintain a separate time-ordered index for expiry so `remove_expired` scans only expired entries rather than the full pool.
3. Apply per-connection or per-IP rate limiting to expensive pool-dump RPC methods at the RPC server layer.

## Proof of Concept
**Step 1 – Fill the pool** (tx-pool submitter):
```python
for i in range(50_000):
    rpc.send_transaction(build_min_size_tx(i))
```

**Step 2 – Saturate the service loop** (RPC caller):
```python
import time, requests
while True:
    t0 = time.time()
    requests.post("http://node:8114", json={
        "jsonrpc": "2.0", "id": 1,
        "method": "get_raw_tx_pool",
        "params": [True]
    })
    print(f"call took {time.time()-t0:.2f}s")
```

**Expected observation**: Concurrent `send_transaction` and `get_block_template` calls queue behind the scan and time out. The `remove_expired` path at `tx-pool/src/pool.rs:271` adds an additional O(n) scan on every new block, compounding the effect. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rpc/src/module/pool.rs (L703-718)
```rust
    fn get_raw_tx_pool(&self, verbose: Option<bool>) -> Result<RawTxPool> {
        let tx_pool = self.shared.tx_pool_controller();

        let raw = if verbose.unwrap_or(false) {
            let info = tx_pool
                .get_all_entry_info()
                .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?;
            RawTxPool::Verbose(info.into())
        } else {
            let ids = tx_pool
                .get_all_ids()
                .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?;
            RawTxPool::Ids(ids.into())
        };
        Ok(raw)
    }
```

**File:** tx-pool/src/pool.rs (L271-288)
```rust
    pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
        let now_ms = ckb_systemtime::unix_time_as_millis();

        let removed: Vec<_> = self
            .pool_map
            .iter()
            .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
            .map(|entry| entry.inner.clone())
            .collect();

        for entry in removed {
            let tx_hash = entry.transaction().hash();
            debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
            self.pool_map.remove_entry(&entry.proposal_short_id());
            let reject = Reject::Expiry(entry.timestamp);
            callbacks.call_reject(self, &entry, reject);
        }
    }
```

**File:** tx-pool/src/pool.rs (L464-487)
```rust
    pub(crate) fn get_all_entry_info(&self) -> TxPoolEntryInfo {
        let pending = self
            .pool_map
            .score_sorted_iter_by_statuses(vec![Status::Pending, Status::Gap])
            .map(|entry| (entry.transaction().hash(), entry.to_info()))
            .collect();

        let proposed = self
            .pool_map
            .sorted_proposed_iter()
            .map(|entry| (entry.transaction().hash(), entry.to_info()))
            .collect();

        let conflicted = self
            .conflicts_cache
            .iter()
            .map(|(_id, tx)| tx.hash())
            .collect();
        TxPoolEntryInfo {
            pending,
            proposed,
            conflicted,
        }
    }
```

**File:** tx-pool/src/component/pool_map.rs (L408-416)
```rust
    pub(crate) fn score_sorted_iter_by_statuses(
        &self,
        statuses: Vec<Status>,
    ) -> impl Iterator<Item = &TxEntry> {
        self.entries
            .iter_by_score()
            .rev()
            .filter_map(move |entry| statuses.contains(&entry.status).then_some(&entry.inner))
    }
```
