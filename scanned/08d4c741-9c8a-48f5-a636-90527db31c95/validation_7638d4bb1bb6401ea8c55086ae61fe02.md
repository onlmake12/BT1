Audit Report

## Title
Unbounded O(N) Iteration in `PoolMap::estimate_fee_rate` Fallback with Unconditional `get_all_entry_info()` Enables RPC-Triggered Pool Traversal - (File: `tx-pool/src/component/pool_map.rs`)

## Summary

The `estimate_fee_rate` RPC handler unconditionally calls `get_all_entry_info()` (iterating all pool entries into allocated HashMaps) before attempting the primary estimator, and when the primary estimator fails (e.g., `Error::NotReady` during IBD), falls back to `PoolMap::estimate_fee_rate`, which performs a second unbounded O(N) traversal of all pool entries. There is no per-call iteration cap, no result caching, and no RPC rate limiting. An unprivileged caller can repeatedly invoke this endpoint to cause sustained CPU and memory pressure on the tx-pool service.

## Finding Description

**Verified call chain (all code confirmed in-repo):**

1. `rpc/src/module/experiment.rs:301–315` — RPC handler, `enable_fallback` defaults to `true` [1](#0-0) 

2. `tx-pool/src/process.rs:950` — `get_all_entry_info()` is called **unconditionally**, before the primary estimator is even attempted. This allocates two `HashMap`s containing every pending/proposed entry. [2](#0-1) 

3. `tx-pool/src/process.rs:956–964` — On any `Err` from the primary estimator (including `Error::NotReady` during IBD, or `Error::Dummy`), the fallback path is taken. [3](#0-2) 

4. `tx-pool/src/pool.rs:557–572` — `TxPool::estimate_fee_rate` validates `target_to_be_committed` in `3..=131` and delegates to `PoolMap::estimate_fee_rate`. [4](#0-3) 

5. `tx-pool/src/component/pool_map.rs:334–359` — The fallback iterates over **every** pool entry via `iter_by_score().rev()` with no upper bound on iterations. The only early-exit condition is `target_blocks` reaching zero, which requires filling enough simulated blocks. With a large pool and high `target_blocks` (up to 131), the entire pool is traversed. [5](#0-4) 

**Why existing checks are insufficient:**

- `TxPoolConfig` bounds the pool by `max_tx_pool_size` (bytes) only — there is no cap on the number of transactions. [6](#0-5) 
- The `3..=131` range check on `target_to_be_committed` only prevents degenerate inputs; it does not limit pool traversal depth. [7](#0-6) 
- No rate limiting exists on the `estimate_fee_rate` RPC endpoint. [1](#0-0) 
- `Error::NotReady` is a defined, expected error state during IBD, making the fallback path always active in that common node state. [8](#0-7) 

## Impact Explanation

Each `estimate_fee_rate` call performs two full O(N) traversals of the pool: one in `get_all_entry_info()` (with HashMap allocation) and one in `PoolMap::estimate_fee_rate`. With a pool at `max_tx_pool_size` filled with small transactions, N can reach tens of thousands. The synchronous iteration inside an async context can delay the tx-pool service's processing of other messages, including `get_block_template`, degrading mining throughput. This matches **Low (501–2000 points): Any other important performance improvements for CKB**. The impact does not reach "High: network congestion with few costs" because filling the pool requires the attacker to control many valid UTXOs (non-trivial cost), and the RPC is bound to localhost by default, limiting remote exploitability.

## Likelihood Explanation

- During IBD (a common and prolonged node state), `Error::NotReady` is always returned by the primary estimator, making the fallback permanently active.
- The `estimate_fee_rate` RPC requires no authentication and no special privileges.
- Filling the pool requires controlling many valid UTXOs, which is a non-trivial but achievable prerequisite for a motivated attacker.
- Repeated rapid calls to the endpoint are trivially scriptable.
- Remote exploitation requires the operator to have exposed the RPC port (non-default), reducing realistic attacker population.

## Recommendation

1. **Gate `get_all_entry_info()` behind a readiness check**: Only call it if the primary estimator signals it is ready; otherwise skip directly to the fallback or return an error.
2. **Add an iteration cap** in `PoolMap::estimate_fee_rate`: limit the loop to at most `target_blocks * (max_block_bytes / min_tx_size)` entries.
3. **Cache the result** of `estimate_fee_rate` with a short TTL (1–2 seconds) to absorb repeated calls.
4. **Rate-limit** the `estimate_fee_rate` RPC endpoint per caller IP.

## Proof of Concept

```bash
# Prerequisite: node in IBD (primary estimator returns NotReady)
# Step 1: Fill pool with small transactions using controlled UTXOs
for i in $(seq 1 20000); do
  curl -s -X POST http://127.0.0.1:8114 \
    -H 'Content-Type: application/json' \
    -d '{"id":1,"jsonrpc":"2.0","method":"send_transaction","params":[<min_size_tx_json>,"passthrough"]}'
done

# Step 2: Hammer estimate_fee_rate — each call triggers two full O(N) pool traversals
while true; do
  curl -s -X POST http://127.0.0.1:8114 \
    -H 'Content-Type: application/json' \
    -d '{"id":1,"jsonrpc":"2.0","method":"estimate_fee_rate","params":[null,true]}'
done
```

Observable effect: measurable CPU spikes and increased latency on `get_block_template` responses during the loop, verifiable by timing both RPCs concurrently.

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

**File:** tx-pool/src/process.rs (L950-954)
```rust
        let all_entry_info = self.tx_pool.read().await.get_all_entry_info();
        match self
            .fee_estimator
            .estimate_fee_rate(estimate_mode, all_entry_info)
        {
```

**File:** tx-pool/src/process.rs (L956-968)
```rust
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

**File:** tx-pool/src/component/pool_map.rs (L342-358)
```rust
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

**File:** util/app-config/src/configs/tx_pool.rs (L12-13)
```rust
    /// Keep the transaction pool below <max_tx_pool_size> mb
    pub max_tx_pool_size: usize,
```

**File:** util/fee-estimator/src/error.rs (L12-13)
```rust
    #[error("not ready")]
    NotReady,
```
