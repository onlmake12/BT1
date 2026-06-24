All code references have been verified against the actual codebase. Every line number, function name, and call chain cited in the claim is confirmed accurate. Here is the validated output:

---

Audit Report

## Title
Unbounded O(N log N) Iteration on Every `estimate_fee_rate` and `get_raw_tx_pool(verbose=true)` RPC Call Enables Tx-Pool Actor Stall — (File: `tx-pool/src/component/pool_map.rs`, `tx-pool/src/process.rs`, `rpc/src/module/experiment.rs`)

## Summary
Every call to `estimate_fee_rate` causes the tx-pool actor to collect and sort all pending, gap, and proposed entries with no per-call cap, performing O(N log N) work inline in the actor's sequential message loop. A second full scan occurs in the fallback path when the primary estimator returns `NotReady`. `get_raw_tx_pool(verbose=true)` shares the same unbounded `get_all_entry_info()` traversal with no pagination. Concurrent RPC calls queue this work serially, delaying block assembly, transaction submission, and relay for the duration.

## Finding Description
**Confirmed call chain:**

1. `ExperimentRpcImpl::estimate_fee_rate` in `rpc/src/module/experiment.rs` L301–315 forwards directly to the tx-pool controller with no guard or rate limit.

2. `TxPoolController::estimate_fee_rate` in `tx-pool/src/service.rs` L407–413 sends a synchronous `EstimateFeeRate` message to the pool actor.

3. `TxPoolService::estimate_fee_rate` in `tx-pool/src/process.rs` L945–970 acquires the pool read-lock and calls `get_all_entry_info()` unconditionally at L950, with no entry count cap.

4. `TxPool::get_all_entry_info` in `tx-pool/src/pool.rs` L464–487 calls `score_sorted_iter_by_statuses` (Pending + Gap) and `sorted_proposed_iter`, walking the entire multi-index map.

5. `Algorithm::estimate_fee_rate` in `util/fee-estimator/src/estimator/weight_units_flow.rs` L173–184 collects all entries into a `Vec` and calls `sort_unstable`, making per-call cost O(N log N).

6. When `is_ready == false` (e.g., post-IBD or fresh start), the fallback path in `tx-pool/src/process.rs` L957–964 acquires the read-lock a second time and calls `PoolMap::estimate_fee_rate` in `tx-pool/src/component/pool_map.rs` L334–358, which iterates all entries again via `iter_by_score().rev()`.

7. `get_raw_tx_pool(verbose=true)` in `rpc/src/module/pool.rs` L703–718 calls `get_all_entry_info()` identically, with no pagination or entry limit.

8. The `Message::EstimateFeeRate` arm in `tx-pool/src/service.rs` L1029–1039 is handled inline in the actor loop, so concurrent RPC calls queue O(N log N) work serially, blocking all other pool operations.

**Pool capacity:** `max_tx_pool_size = 180_000_000` bytes (`resource/ckb.toml` L211). With minimum-size transactions (~200 bytes), the pool can hold on the order of hundreds of thousands of entries.

**No rate limiting:** No per-IP throttling, per-call entry caps, or authentication exists on these endpoints.

## Impact Explanation
The concrete impact is sustained performance degradation of a single node's tx-pool service: block assembly responses, transaction submission, and relay are delayed proportionally to pool occupancy and call rate. This does not crash the node, cause consensus deviation, or damage the CKB economy directly. The correct classification is **Low (501–2000 points): Any other important performance improvements for CKB**. The "High" severity is not supported because the effect is latency/throughput degradation on one node, not a node crash or network-wide congestion.

## Likelihood Explanation
- The RPC binds to `127.0.0.1:8114` by default (`resource/ckb.toml` L182), limiting external access unless the operator explicitly exposes it — a common production configuration for public RPC nodes.
- Filling the pool requires paying actual transaction fees and owning sufficient UTXOs, imposing a non-trivial one-time economic cost.
- The fallback path (`NotReady`) is reachable immediately after node start or IBD exit, making the double-scan available without special timing or pool-filling cost.
- No authentication, rate limiting, or entry cap exists on either endpoint once the RPC is reachable.

## Recommendation
1. **Cap per-call iteration** — Add a configurable `max_entries_per_fee_estimate` limit; sample or truncate the pool map before sorting rather than collecting all entries.
2. **Paginate `get_raw_tx_pool(verbose=true)`** — Accept `limit`/`offset` parameters and enforce a hard maximum per call.
3. **Rate-limit expensive RPC methods** — Apply a per-IP or global call-rate limit to `estimate_fee_rate` and `get_raw_tx_pool` at the RPC layer.
4. **Decouple estimation from the actor loop** — Perform the sort and estimation on a snapshot copied outside the actor so the message queue is not blocked during O(N log N) work.

## Proof of Concept
```bash
# Step 1: Fill the tx-pool with many minimum-fee transactions
for i in $(seq 1 50000); do
    ckb-cli tx send --tx <min_fee_tx_$i>
done

# Step 2: Flood estimate_fee_rate from any client with RPC access
while true; do
    curl -s -X POST http://<node>:8114 \
      -H 'Content-Type: application/json' \
      -d '{"id":1,"jsonrpc":"2.0","method":"estimate_fee_rate","params":[]}' &
done
```
With ~50,000 pool entries, each call forces a full O(N log N) sort inside the actor. Concurrent calls queue serially, measurably increasing latency for `get_block_template` and `send_transaction` responses. The `get_raw_tx_pool(verbose=true)` variant additionally allocates a proportionally large JSON payload, amplifying memory pressure. The `NotReady` fallback path (reachable immediately post-IBD) triggers a second full scan without requiring any pool-filling cost.

---

**Code references confirmed:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

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

**File:** tx-pool/src/service.rs (L407-413)
```rust
    pub fn estimate_fee_rate(
        &self,
        estimate_mode: EstimateMode,
        enable_fallback: bool,
    ) -> Result<FeeEstimatesResult, AnyError> {
        send_message!(self, EstimateFeeRate, (estimate_mode, enable_fallback))
    }
```

**File:** tx-pool/src/service.rs (L1029-1039)
```rust
        Message::EstimateFeeRate(Request {
            responder,
            arguments: (estimate_mode, enable_fallback),
        }) => {
            let fee_estimates_result = service
                .estimate_fee_rate(estimate_mode, enable_fallback)
                .await;
            if let Err(e) = responder.send(fee_estimates_result) {
                error!("Responder sending fee_estimates_result failed {:?}", e)
            };
        }
```

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

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L173-184)
```rust
        let sorted_current_txs = {
            let mut current_txs: Vec<_> = all_entry_info
                .pending
                .into_values()
                .chain(all_entry_info.proposed.into_values())
                .map(TxStatus::new_from_entry_info)
                .collect();
            current_txs.sort_unstable_by(|a, b| b.cmp(a));
            current_txs
        };

        self.do_estimate(target_blocks, &sorted_current_txs)
```

**File:** tx-pool/src/component/pool_map.rs (L334-358)
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
```

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

**File:** resource/ckb.toml (L182-182)
```text
listen_address = "127.0.0.1:8114" # {{
```

**File:** resource/ckb.toml (L211-211)
```text
max_tx_pool_size = 180_000_000 # 180mb
```
