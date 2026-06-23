### Title
Unbounded Iteration Over All Tx-Pool Entries in `estimate_fee_rate` Fallback Enables RPC-Triggered CPU Exhaustion - (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

The `estimate_fee_rate` RPC endpoint, when its primary estimator is unavailable (e.g., during IBD or before sufficient historical data is collected), falls back to `PoolMap::estimate_fee_rate`, which iterates over **every entry** in the tx pool without any per-call iteration cap. An unprivileged RPC caller can first flood the pool with many small transactions and then repeatedly invoke `estimate_fee_rate` to trigger this unbounded loop, causing sustained CPU exhaustion and blocking the single-threaded tx-pool service actor.

---

### Finding Description

**Call chain:**

1. `ExperimentRpcImpl::estimate_fee_rate` (RPC handler) — `rpc/src/module/experiment.rs:301–315`
2. → `TxPoolService::estimate_fee_rate` — `tx-pool/src/process.rs:945–970`
3. → `TxPool::estimate_fee_rate` (fallback path) — `tx-pool/src/pool.rs:557–572`
4. → `PoolMap::estimate_fee_rate` — `tx-pool/src/component/pool_map.rs:334–359`

The fallback is activated whenever the primary fee estimator returns `Err` (e.g., `Error::NotReady` during IBD, or when using the `Dummy` estimator), and `enable_fallback` is `true` (the default). [1](#0-0) 

Inside the fallback, `PoolMap::estimate_fee_rate` iterates over **all** pool entries sorted by score: [2](#0-1) 

The loop has no upper bound on the number of entries visited. It only exits early if `target_blocks` reaches zero, which requires filling enough simulated blocks. With a large pool and a high `target_blocks` value (up to 131 per the validation in `TxPool::estimate_fee_rate`), the loop can traverse the entire pool. [3](#0-2) 

Additionally, the primary estimator path calls `get_all_entry_info()` unconditionally before attempting estimation, which also iterates over all pool entries: [4](#0-3) [5](#0-4) 

The tx pool is bounded in **bytes** by `max_tx_pool_size`, but the **number of transactions** is not directly capped. Many small transactions can fill the pool, maximizing the iteration count per call.

---

### Impact Explanation

- **CPU exhaustion**: Each `estimate_fee_rate` call with a large pool triggers O(N) work where N is the number of pool entries. Repeated calls amplify this.
- **Tx-pool service blocking**: The tx-pool service processes messages sequentially. A slow `estimate_fee_rate` call holds the read lock on the pool and delays all other pool operations (transaction submission, block assembly, reorg handling).
- **Miner degradation**: Block template assembly (`get_block_template`) is also served by the tx-pool service and can be delayed, degrading mining throughput. [6](#0-5) 

---

### Likelihood Explanation

- The `estimate_fee_rate` RPC is publicly documented and accessible to any RPC caller (local or remote if the RPC port is exposed).
- During IBD — a common node state — the `WeightUnitsFlow` and `ConfirmationFraction` estimators return `NotReady`, making the fallback path always active.
- Submitting many small transactions to fill the pool is straightforward for any `send_transaction` caller.
- The attack requires no special privileges, no keys, and no majority hashpower. [7](#0-6) 

---

### Recommendation

1. **Add a per-call iteration cap** in `PoolMap::estimate_fee_rate`. For example, limit the loop to at most `max_block_bytes / min_tx_size * target_blocks` entries, or a fixed constant (e.g., 10,000).
2. **Cache the result** of `estimate_fee_rate` with a short TTL (e.g., 1–2 seconds) so repeated calls do not re-traverse the pool.
3. **Rate-limit** the `estimate_fee_rate` RPC endpoint per caller IP.
4. Avoid calling `get_all_entry_info()` unconditionally before the primary estimator check; gate it behind a readiness check first.

---

### Proof of Concept

```
# Step 1: Fill the tx pool with many small transactions
for i in $(seq 1 50000); do
  curl -s -X POST http://localhost:8114 \
    -H 'Content-Type: application/json' \
    -d '{"id":1,"jsonrpc":"2.0","method":"send_transaction","params":[<small_tx_json>,"passthrough"]}'
done

# Step 2: Repeatedly call estimate_fee_rate to trigger unbounded loop
while true; do
  curl -s -X POST http://localhost:8114 \
    -H 'Content-Type: application/json' \
    -d '{"id":1,"jsonrpc":"2.0","method":"estimate_fee_rate","params":[null,true]}'
done
```

Each call in Step 2 triggers `PoolMap::estimate_fee_rate` to iterate over all N pool entries. With a pool at `max_tx_pool_size` filled with minimum-size transactions, N can be in the tens of thousands, causing measurable CPU spikes and tx-pool service latency on every call. [8](#0-7) [9](#0-8)

### Citations

**File:** tx-pool/src/process.rs (L945-970)
```rust
    pub(crate) async fn estimate_fee_rate(
        &self,
        estimate_mode: EstimateMode,
        enable_fallback: bool,
    ) -> Result<FeeRate, AnyError> {
        let all_entry_info = self.tx_pool.read().await.get_all_entry_info();
        match self
            .fee_estimator
            .estimate_fee_rate(estimate_mode, all_entry_info)
        {
            Ok(fee_rate) => Ok(fee_rate),
            Err(err) => {
                if enable_fallback {
                    let target_blocks =
                        FeeEstimator::target_blocks_for_estimate_mode(estimate_mode);
                    self.tx_pool
                        .read()
                        .await
                        .estimate_fee_rate(target_blocks)
                        .map_err(Into::into)
                } else {
                    Err(err.into())
                }
            }
        }
    }
```

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

**File:** tx-pool/src/pool.rs (L557-572)
```rust
    pub(crate) fn estimate_fee_rate(
        &self,
        target_to_be_committed: BlockNumber,
    ) -> Result<FeeRate, FeeEstimatorError> {
        if !(3..=131).contains(&target_to_be_committed) {
            return Err(FeeEstimatorError::NoProperFeeRate);
        }
        let fee_rate = self.pool_map.estimate_fee_rate(
            (target_to_be_committed - self.snapshot.consensus().tx_proposal_window().closest())
                as usize,
            self.snapshot.consensus().max_block_bytes() as usize,
            self.snapshot.consensus().max_block_cycles(),
            self.config.min_fee_rate,
        );
        Ok(fee_rate)
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
