### Title
Unbounded Linear Scan Over All Pool Entries in `estimate_fee_rate` Fallback Enables RPC-Triggered CPU/Lock DOS — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

The fallback path of the `estimate_fee_rate` RPC performs an unbounded linear scan over every entry in the tx-pool. An unprivileged attacker who fills the pool with many minimum-fee, minimum-size transactions forces every subsequent `estimate_fee_rate` call to iterate the entire pool while holding the pool's read lock, degrading or blocking concurrent pool operations for legitimate users.

---

### Finding Description

`PoolMap::estimate_fee_rate` in `tx-pool/src/component/pool_map.rs` iterates over the full sorted entry set with no cap on the number of entries visited:

```rust
pub(crate) fn estimate_fee_rate(
    &self,
    mut target_blocks: usize,
    max_block_bytes: usize,
    max_block_cycles: Cycle,
    min_fee_rate: FeeRate,
) -> FeeRate {
    let iter = self.entries.iter_by_score().rev();   // full pool scan
    for entry in iter {
        current_block_bytes += entry.inner.size;
        current_block_cycles += entry.inner.cycles;
        if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
            target_blocks -= 1;
            if target_blocks == 0 { return entry.inner.fee_rate(); }
            ...
        }
    }
    min_fee_rate   // reached only after scanning every entry
}
``` [1](#0-0) 

The only early-exit is when `target_blocks` decrements to zero, which requires accumulating `target_blocks × max_block_bytes` (or cycles) worth of entries. An attacker who submits many tiny, minimum-fee transactions ensures that each individual entry contributes negligible bytes/cycles, so the loop must visit **every entry in the pool** before the byte/cycle threshold is crossed even once — causing the function to fall through to `return min_fee_rate` after a full O(n) scan.

This function is invoked synchronously inside the tx-pool service's async message handler while holding the pool's `RwLock` read guard:

```rust
Message::GetAllEntryInfo(Request { responder, .. }) => {
    let tx_pool = service.tx_pool.read().await;   // read lock held
    let info = tx_pool.get_all_entry_info();
    ...
}
``` [2](#0-1) 

The `estimate_fee_rate` RPC is exposed publicly with no rate-limiting and defaults `enable_fallback = true`, meaning the pool-scan path is reachable by any RPC caller:

```rust
fn estimate_fee_rate(
    &self,
    estimate_mode: Option<EstimateMode>,
    enable_fallback: Option<bool>,
) -> Result<Uint64> {
    let enable_fallback = enable_fallback.unwrap_or(true);
    self.shared.tx_pool_controller()
        .estimate_fee_rate(estimate_mode.into(), enable_fallback)
        ...
}
``` [3](#0-2) 

A parallel unbounded scan exists in `get_all_entry_info` / `get_ids`, which collect every pending, gap, and proposed entry into heap-allocated maps with no pagination:

```rust
pub(crate) fn get_all_entry_info(&self) -> TxPoolEntryInfo {
    let pending = self.pool_map
        .score_sorted_iter_by_statuses(vec![Status::Pending, Status::Gap])
        .map(|entry| (entry.transaction().hash(), entry.to_info()))
        .collect();          // unbounded collect
    let proposed = self.pool_map.sorted_proposed_iter()
        .map(|entry| (entry.transaction().hash(), entry.to_info()))
        .collect();          // unbounded collect
    ...
}
``` [4](#0-3) 

---

### Impact Explanation

While the pool is bounded by `max_tx_pool_size`, within that bound an attacker can pack the pool with the maximum number of minimum-size transactions. Each `estimate_fee_rate` call then:

1. Performs a full O(n) CPU scan over all entries.
2. Holds the pool read lock for the duration, serialising against any concurrent write (e.g., new transaction admission, block commit, pool eviction).
3. Allocates proportional memory for the response.

Repeated calls from an unauthenticated RPC client (or a script that calls the RPC in a tight loop) can saturate the pool service's async executor, delay transaction propagation, and degrade block-assembly latency — all without the attacker's transactions ever being mined, since they can be replaced or evicted.

---

### Likelihood Explanation

- The `estimate_fee_rate` RPC endpoint is publicly documented and enabled by default.
- No authentication, rate-limiting, or per-call entry cap is enforced.
- Filling the pool with minimum-fee transactions is cheap relative to the disruption caused; the attacker's cost scales with `max_tx_pool_size / min_tx_size`, while the victim's cost per RPC call scales with the same quantity.
- The fallback algorithm is active by default (`enable_fallback = true`), so no special parameter is needed to trigger the scan.

---

### Recommendation

1. **Cap the scan in `estimate_fee_rate`**: introduce a `max_entries` parameter (e.g., proportional to `target_blocks × expected_txs_per_block`) and break the loop once that many entries have been visited, returning `min_fee_rate` early.

```rust
let max_entries = target_blocks * MAX_BLOCK_PROPOSALS_LIMIT;
for entry in iter.take(max_entries) { ... }
```

2. **Paginate `get_raw_tx_pool` / `get_all_entry_info`**: add an optional `limit`/`cursor` parameter so callers cannot force a full pool serialisation in a single call.

3. **Rate-limit the `estimate_fee_rate` and `get_raw_tx_pool` RPC endpoints** at the server layer to bound the aggregate scan rate per client.

---

### Proof of Concept

**Attacker-controlled entry path:**

1. Attacker submits `N` transactions, each of minimum size (e.g., 100 bytes) and minimum fee rate, until the pool is at capacity. Each transaction is valid and accepted by the pool.
2. Attacker (or any RPC client) calls `estimate_fee_rate` in a tight loop. Because each transaction contributes ~100 bytes and `max_block_bytes` is ~500 KB, the loop must visit ~5 000 entries before the byte threshold is crossed once. With `target_blocks = 2` (medium priority), the loop visits ~10 000 entries before returning — or scans the entire pool if the threshold is never crossed.
3. Each call holds the pool read lock for the full scan duration. Concurrent `submit_transaction` calls that need the write lock are queued behind every in-flight `estimate_fee_rate` call.
4. A legitimate user submitting a transaction experiences increased latency or timeout proportional to the number of concurrent attacker RPC calls. [1](#0-0) [5](#0-4) [4](#0-3)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L334-359)
```rust
    pub(crate) fn estimate_fee_rate(
        &self,
        mut target_blocks: usize,
        max_block_bytes: usize,
        max_block_cycles: Cycle,
        min_fee_rate: FeeRate,
    ) -> FeeRate {
        debug_assert!(target_blocks > 0);
        let iter = self.entries.iter_by_score().rev();
        let mut current_block_bytes = 0;
        let mut current_block_cycles = 0;
        for entry in iter {
            current_block_bytes += entry.inner.size;
            current_block_cycles += entry.inner.cycles;
            if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
                target_blocks -= 1;
                if target_blocks == 0 {
                    return entry.inner.fee_rate();
                }
                current_block_bytes = entry.inner.size;
                current_block_cycles = entry.inner.cycles;
            }
        }

        min_fee_rate
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

**File:** rpc/src/module/experiment.rs (L215-220)
```rust
    #[rpc(name = "estimate_fee_rate")]
    fn estimate_fee_rate(
        &self,
        estimate_mode: Option<EstimateMode>,
        enable_fallback: Option<bool>,
    ) -> Result<Uint64>;
```

**File:** rpc/src/module/experiment.rs (L301-315)
```rust
    fn estimate_fee_rate(
        &self,
        estimate_mode: Option<EstimateMode>,
        enable_fallback: Option<bool>,
    ) -> Result<Uint64> {
        let estimate_mode = estimate_mode.unwrap_or_default();
        let enable_fallback = enable_fallback.unwrap_or(true);
        self.shared
            .tx_pool_controller()
            .estimate_fee_rate(estimate_mode.into(), enable_fallback)
            .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?
            .map_err(RPCError::from_any_error)
            .map(core::FeeRate::as_u64)
            .map(Into::into)
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
