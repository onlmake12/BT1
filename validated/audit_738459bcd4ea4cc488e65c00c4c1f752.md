Audit Report

## Title
Unbounded Iteration Over All Pool Entries in `get_raw_tx_pool` RPC Enables Node Resource Exhaustion — (File: `rpc/src/module/pool.rs`)

## Summary
The `get_raw_tx_pool` RPC handler with `verbose=true` invokes `get_all_entry_info()`, which performs three full, uncapped scans of the transaction pool — pending/gap, proposed, and conflicted entries — collecting all results into memory with no pagination or result-size limit. With a default pool capacity of 180 MB, the pool can hold hundreds of thousands of entries. Repeated calls force unbounded CPU work, large memory allocation, and prolonged async read-lock holding on the tx-pool, which serializes against all write-path operations including block assembly and transaction relay.

## Finding Description
In `rpc/src/module/pool.rs` at L703–718, `get_raw_tx_pool` dispatches to `get_all_entry_info()` with no limit on entries returned. [1](#0-0) 

In `tx-pool/src/service.rs` at L1000–1006, the `GetAllEntryInfo` message handler acquires an async read lock on the pool and calls `get_all_entry_info()`, holding the lock for the entire duration of the scan. [2](#0-1) 

In `tx-pool/src/pool.rs` at L464–487, `get_all_entry_info()` performs three full unbounded scans — `score_sorted_iter_by_statuses` over pending/gap entries, `sorted_proposed_iter` over proposed entries, and `conflicts_cache.iter()` over conflicted entries — collecting all results into `HashMap`s with no cap. [3](#0-2) 

One partial mitigation exists: `conflicts_cache` is an LRU cache capped at `CONFLICTES_CACHE_SIZE = 10_000` entries, so the conflicted scan is bounded. [4](#0-3) [5](#0-4) 

However, the primary pool (`pool_map`) holding pending and proposed entries is bounded only by `max_tx_pool_size = 180_000_000` bytes (180 MB), with no per-call iteration cap. [6](#0-5) 

No rate limiting, authentication, or pagination exists anywhere in the RPC layer for this endpoint. [1](#0-0) 

The `TxPoolConfig` struct confirms `max_tx_pool_size` is the only bound on pool size, with no per-RPC-call limit. [7](#0-6) 

## Impact Explanation
With a 180 MB pool and realistic minimum CKB transaction sizes (~200–500 bytes), the pool can hold on the order of 360,000–900,000 entries. Each `get_raw_tx_pool?verbose=true` call forces O(n) CPU iteration, O(n) memory allocation for the full `TxPoolEntryInfo` response (hashes, cycles, fees, ancestor metadata per entry), and holds the async read lock for the entire duration. Concurrent write operations — new transaction submissions, block processing updates — require a write lock and are blocked while any read lock is held. Sustained rapid calls can exhaust node RAM and CPU, making the RPC service unresponsive and degrading or halting block assembly (`get_block_template`) and transaction relay. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
The attack requires two steps: (1) fill the pool by submitting many small transactions paying the minimum fee rate (1,000 shannons/KB); filling 180 MB costs approximately 1.8 CKB — a low economic barrier; (2) repeatedly invoke `get_raw_tx_pool?verbose=true` via the RPC. By default the RPC listens on `127.0.0.1:8114`, requiring local access, but operators commonly expose the RPC to broader networks for monitoring, widening the attack surface. No rate limiting, authentication, or pagination exists on this endpoint.

## Recommendation
- Add `offset` and `limit` pagination parameters to `get_raw_tx_pool` so callers cannot retrieve the entire pool in a single call.
- Alternatively, impose a hard cap on the maximum number of entries returned per call (e.g., 10,000) and document the limit.
- Consider per-IP or per-connection rate limiting on expensive RPC methods.
- Document that operators should restrict RPC access to trusted clients when the pool is large.

## Proof of Concept
1. Configure a node with `min_fee_rate = 1000` and `max_tx_pool_size = 180_000_000` (defaults).
2. Submit a large number of minimal-size transactions via `send_transaction` or P2P relay until the pool approaches its size limit (~1.8 CKB in fees).
3. Repeatedly call:
   ```json
   {"id":1,"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[true]}
   ```
4. Observe: each call triggers a full scan of all pool entries in `get_all_entry_info()` at `tx-pool/src/pool.rs:464–487`, holds the pool read lock for the duration, allocates memory proportional to pool size, and returns a response that can be hundreds of megabytes. Sustained repetition exhausts node CPU and RAM and blocks concurrent pool write operations.

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

**File:** tx-pool/src/service.rs (L1000-1006)
```rust
        Message::GetAllEntryInfo(Request { responder, .. }) => {
            let tx_pool = service.tx_pool.read().await;
            let info = tx_pool.get_all_entry_info();
            if let Err(e) = responder.send(info) {
                error!("Responder sending get_all_entry_info failed {:?}", e)
            };
        }
```

**File:** tx-pool/src/pool.rs (L31-31)
```rust
const CONFLICTES_CACHE_SIZE: usize = 10_000;
```

**File:** tx-pool/src/pool.rs (L65-65)
```rust
            conflicts_cache: LruCache::new(CONFLICTES_CACHE_SIZE),
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

**File:** resource/ckb.toml (L211-211)
```text
max_tx_pool_size = 180_000_000 # 180mb
```

**File:** util/app-config/src/configs/tx_pool.rs (L11-13)
```rust
pub struct TxPoolConfig {
    /// Keep the transaction pool below <max_tx_pool_size> mb
    pub max_tx_pool_size: usize,
```
