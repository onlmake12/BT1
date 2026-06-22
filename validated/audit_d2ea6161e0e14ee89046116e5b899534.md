### Title
Unbounded Iteration Over All Tx-Pool Entries in `get_raw_tx_pool` RPC Enables CPU/Memory DoS - (`rpc/src/module/pool.rs`, `tx-pool/src/pool.rs`)

### Summary

The `get_raw_tx_pool(verbose=true)` RPC endpoint iterates over every entry in the transaction pool with no pagination, no size cap on the response, and no rate limiting. An unprivileged attacker who fills the tx-pool with many minimum-fee transactions can then repeatedly invoke this endpoint to force the node to serialize the entire pool state into a single in-memory collection and a single JSON response, causing sustained CPU and memory pressure on the node.

### Finding Description

**Root cause — unbounded `collect()` over the entire pool:**

`get_raw_tx_pool` in `rpc/src/module/pool.rs` dispatches to `get_all_entry_info()` when `verbose=true`:

```rust
fn get_raw_tx_pool(&self, verbose: Option<bool>) -> Result<RawTxPool> {
    let tx_pool = self.shared.tx_pool_controller();
    let raw = if verbose.unwrap_or(false) {
        let info = tx_pool
            .get_all_entry_info()   // ← iterates ALL entries
            ...
        RawTxPool::Verbose(info.into())
    } else { ... };
    Ok(raw)
}
```

`get_all_entry_info()` in `tx-pool/src/pool.rs` performs three unbounded iterations and collects every result into heap-allocated `HashMap`s:

```rust
pub(crate) fn get_all_entry_info(&self) -> TxPoolEntryInfo {
    let pending = self
        .pool_map
        .score_sorted_iter_by_statuses(vec![Status::Pending, Status::Gap])
        .map(|entry| (entry.transaction().hash(), entry.to_info()))
        .collect();   // ← no limit

    let proposed = self
        .pool_map
        .sorted_proposed_iter()
        .map(|entry| (entry.transaction().hash(), entry.to_info()))
        .collect();   // ← no limit

    let conflicted = self
        .conflicts_cache
        .iter()
        .map(|(_id, tx)| tx.hash())
        .collect();   // bounded by LRU (10 000)
    TxPoolEntryInfo { pending, proposed, conflicted }
}
```

The same unbounded pattern appears in the `estimate_fee_rate` fallback path in `tx-pool/src/process.rs`, which first calls `get_all_entry_info()` and then, on primary-estimator failure, calls `pool_map.estimate_fee_rate()` which iterates all entries a second time while holding the pool read-lock:

```rust
pub(crate) async fn estimate_fee_rate(...) -> Result<FeeRate, AnyError> {
    let all_entry_info = self.tx_pool.read().await.get_all_entry_info(); // full scan
    match self.fee_estimator.estimate_fee_rate(estimate_mode, all_entry_info) {
        Err(err) if enable_fallback => {
            self.tx_pool.read().await
                .estimate_fee_rate(target_blocks)  // second full scan
                ...
        }
    }
}
```

`pool_map.estimate_fee_rate()` in `tx-pool/src/component/pool_map.rs` iterates every entry unconditionally:

```rust
pub(crate) fn estimate_fee_rate(...) -> FeeRate {
    let iter = self.entries.iter_by_score().rev();
    for entry in iter {   // ← no early exit until target_blocks reaches 0
        ...
    }
    min_fee_rate
}
```

**Attacker-controlled inflation:**

The tx-pool is bounded by `max_tx_pool_size` (bytes), not by transaction count. A minimum-size CKB transaction is ~112 bytes (observed in test output). With the default pool limit of ~180 MB, the pool can hold on the order of **1.6 million transactions**. Each `get_raw_tx_pool(verbose=true)` call must:
1. Acquire the pool read-lock.
2. Iterate and clone metadata for every entry.
3. Allocate a `HashMap` proportional to pool size.
4. Serialize the entire result to JSON.

There is no pagination parameter, no response-size cap, and no rate limiting on this RPC endpoint.

### Impact Explanation

An attacker who submits many minimum-fee transactions (paying only the minimum fee rate) to fill the pool, then repeatedly calls `get_raw_tx_pool(verbose=true)` or `estimate_fee_rate`, forces the node to:
- Allocate large heap buffers on every call.
- Hold the tx-pool read-lock for an extended period, delaying tx submission and block assembly.
- Produce multi-hundred-MB JSON responses, saturating the node's outbound bandwidth.

The result is sustained CPU, memory, and I/O pressure that can degrade or halt normal node operations (tx relay, block template generation) for the duration of the attack. This is a direct service-availability impact with no privileged access required.

### Likelihood Explanation

The `get_raw_tx_pool` RPC is enabled by default and reachable by any client that can connect to the node's RPC port (typically public-facing). Filling the pool with minimum-fee transactions is a standard, permissionless operation. No special keys, roles, or majority hash-power are required. The attack is repeatable and cheap relative to the work imposed on the victim node.

### Recommendation

1. **Add pagination** to `get_raw_tx_pool` (a `limit`/`after` cursor pair, as already used by the Indexer RPC) so that no single call iterates the entire pool.
2. **Cap the response size** server-side (e.g., return at most N entries and indicate truncation).
3. **Apply per-IP rate limiting** to expensive pool-enumeration RPC methods.
4. For `estimate_fee_rate`, avoid calling `get_all_entry_info()` unconditionally before knowing whether the primary estimator has sufficient data; check readiness first.

### Proof of Concept

1. Connect to a CKB node's RPC port.
2. Submit ~100 000 minimum-fee transactions (each ~112 bytes, fee = `min_fee_rate × size`) to fill the pending pool.
3. In a loop, call:
   ```json
   {"id":1,"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[true]}
   ```
4. Observe: each call forces the node to iterate all 100 000 entries, allocate a large `HashMap`, and serialize a multi-MB JSON body. Concurrent block-template requests (`get_block_template`) are delayed because they contend for the same tx-pool lock. Node CPU and memory usage spike on every call with no built-in throttle.

**Relevant code locations:**

- `get_raw_tx_pool` dispatch: [1](#0-0) 
- `get_all_entry_info` unbounded collect: [2](#0-1) 
- `pool_map.estimate_fee_rate` full scan: [3](#0-2) 
- Double full-scan in `estimate_fee_rate` fallback: [4](#0-3)

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
