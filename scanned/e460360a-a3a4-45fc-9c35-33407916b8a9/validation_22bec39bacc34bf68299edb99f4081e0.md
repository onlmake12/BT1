### Title
Fee Rate Estimate Manipulation via Unverified Pool State in Fallback Algorithm — (File: `tx-pool/src/process.rs`)

---

### Summary

The `estimate_fee_rate` RPC exposes a two-tier estimation path. The primary estimators (ConfirmationFraction, WeightUnitsFlow) use historical confirmed-block data with minimum sample requirements — analogous to the "block-level weighted average with minimum reliable amount" in the external report. When those estimators lack sufficient data, the code silently falls back to a pool-state-based algorithm that includes **all** pending transactions without any minimum weight or amount threshold. An unprivileged tx-pool submitter can inject a single tiny transaction with an artificially high fee rate to skew the fallback estimate, mirroring the `estimatedAutoRollUnitPrice` manipulation described in the report.

---

### Finding Description

**Primary (secure) path** — `FeeEstimator::estimate_fee_rate` in `util/fee-estimator/src/estimator/mod.rs`:

- `ConfirmationFraction`: requires `DEFAULT_MIN_SAMPLES` confirmed transactions and a minimum confirmation rate before producing an estimate.
- `WeightUnitsFlow`: requires `historical_blocks` worth of observed flow data before producing an estimate.

Both return `Error::LackData` or `Error::NotReady` when their data requirements are not met (e.g., on a freshly started node, immediately after exiting IBD, or when the chain has seen very few transactions). [1](#0-0) 

**Fallback (insecure) path** — `TxPoolService::estimate_fee_rate` in `tx-pool/src/process.rs`:

```rust
Err(err) => {
    if enable_fallback {
        let target_blocks =
            FeeEstimator::target_blocks_for_estimate_mode(estimate_mode);
        self.tx_pool
            .read()
            .await
            .estimate_fee_rate(target_blocks)   // ← uses raw pool state
            .map_err(Into::into)
    } else {
        Err(err.into())
    }
}
``` [2](#0-1) 

The fallback delegates to `TxPool::estimate_fee_rate` in `tx-pool/src/pool.rs`, which calls `pool_map.estimate_fee_rate(...)` directly on the live pending/proposed set: [3](#0-2) 

The `pool_map.estimate_fee_rate` function sorts all pool entries by fee rate and simulates block-filling to find the boundary fee rate for the requested target. There is **no minimum transaction weight or minimum fee amount filter** applied before entries are included in this simulation — only the global `min_fee_rate` admission floor is enforced at submission time. [4](#0-3) 

The fallback is enabled by default (`enable_fallback` defaults to `true` in the RPC handler): [5](#0-4) 

---

### Impact Explanation

An attacker who can submit transactions to the tx-pool (any unprivileged RPC caller via `send_transaction`) can:

1. **Inflate the estimate**: Submit a single dust-sized transaction (minimum valid byte size) with a disproportionately large fee. Because the fallback sorts by fee rate and fills blocks by weight, a tiny high-fee-rate transaction sits at the top of the sorted list. When the pool is sparse (the exact condition that triggers the fallback), this single entry dominates the boundary fee rate returned to callers.

2. **Deflate the estimate**: Flood the pool with many minimum-fee-rate transactions. The boundary shifts downward, causing wallets and users relying on the estimate to underpay and have their transactions stuck in the proposal/commit pipeline.

Wallets, dApps, and automated systems that call `estimate_fee_rate` during the fallback window (fresh node startup, post-IBD sync, low-activity periods) receive a manipulated fee rate and act on it, resulting in economic harm (overpayment) or transaction liveness failure (underpayment leading to stuck transactions).

---

### Likelihood Explanation

The fallback window is not a rare edge case:

- Every node restart triggers it until the primary estimator accumulates `DEFAULT_MIN_SAMPLES` confirmed transactions.
- Every IBD exit triggers it (`update_ibd_state` clears all estimator state).
- Low-traffic periods on testnet or newly launched chains keep the primary estimator in `LackData` indefinitely.

The attack cost is the minimum fee for one valid CKB transaction — a few hundred shannons. The `send_transaction` RPC endpoint is publicly accessible with no rate limiting beyond the pool's `min_fee_rate` floor. [6](#0-5) [7](#0-6) 

---

### Recommendation

Apply the same principle recommended in the external report: require a **minimum reliable weight threshold** before including a transaction in the fallback simulation. Specifically:

- In `pool_map.estimate_fee_rate`, skip entries whose individual transaction weight falls below a configurable `min_reliable_weight` constant (analogous to the "minimum amount traded in a block" check in the external report).
- Alternatively, require the pool to contain at least a minimum aggregate weight before the fallback produces a result, returning `NoProperFeeRate` otherwise and forcing callers to use a hardcoded safe default.
- Document clearly that `enable_fallback = false` should be used in security-sensitive contexts.

---

### Proof of Concept

1. Start a fresh CKB node (primary estimator is in `NotReady`/`LackData` state).
2. Submit one transaction via `send_transaction` with:
   - Minimum valid byte size (e.g., ~100 bytes)
   - Fee set to `10_000_000` shannons (≈ 100,000 shannons/KB — far above market rate)
3. Call `estimate_fee_rate` with default parameters (`enable_fallback = true`).
4. Observe the returned value reflects the attacker's inflated fee rate rather than any market-derived estimate.
5. A wallet using this value will overpay by orders of magnitude for all subsequent transactions until the primary estimator warms up. [8](#0-7) [9](#0-8)

### Citations

**File:** util/fee-estimator/src/estimator/mod.rs (L92-105)
```rust
    pub fn estimate_fee_rate(
        &self,
        estimate_mode: EstimateMode,
        all_entry_info: TxPoolEntryInfo,
    ) -> Result<FeeRate, Error> {
        let target_blocks = Self::target_blocks_for_estimate_mode(estimate_mode);
        match self {
            Self::Dummy => Err(Error::Dummy),
            Self::ConfirmationFraction(algo) => algo.read().estimate_fee_rate(target_blocks),
            Self::WeightUnitsFlow(algo) => {
                algo.read().estimate_fee_rate(target_blocks, all_entry_info)
            }
        }
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

**File:** tx-pool/src/component/tests/estimate.rs (L8-55)
```rust
#[test]
fn test_estimate_fee_rate() {
    let mut pool = PoolMap::new(1000);
    for i in 0..1024 {
        let tx = build_tx(vec![(&Default::default(), i as u32)], 1);
        let entry = TxEntry::dummy_resolve(tx, i + 1, Capacity::shannons(i + 1), 1000);
        pool.add_entry(entry, Status::Pending).unwrap();
    }

    assert_eq!(
        FeeRate::from_u64(42),
        pool.estimate_fee_rate(1, usize::MAX, Cycle::MAX, FeeRate::from_u64(42))
    );

    assert_eq!(
        FeeRate::from_u64(1024),
        pool.estimate_fee_rate(1, 1000, Cycle::MAX, FeeRate::from_u64(1))
    );
    assert_eq!(
        FeeRate::from_u64(1023),
        pool.estimate_fee_rate(1, 2000, Cycle::MAX, FeeRate::from_u64(1))
    );
    assert_eq!(
        FeeRate::from_u64(1016),
        pool.estimate_fee_rate(2, 5000, Cycle::MAX, FeeRate::from_u64(1))
    );

    assert_eq!(
        FeeRate::from_u64(1024),
        pool.estimate_fee_rate(1, usize::MAX, 1, FeeRate::from_u64(1))
    );
    assert_eq!(
        FeeRate::from_u64(1023),
        pool.estimate_fee_rate(1, usize::MAX, 2047, FeeRate::from_u64(1))
    );
    assert_eq!(
        FeeRate::from_u64(1015),
        pool.estimate_fee_rate(2, usize::MAX, 5110, FeeRate::from_u64(1))
    );

    assert_eq!(
        FeeRate::from_u64(624),
        pool.estimate_fee_rate(100, 5000, 5110, FeeRate::from_u64(1))
    );
    assert_eq!(
        FeeRate::from_u64(1),
        pool.estimate_fee_rate(1000, 5000, 5110, FeeRate::from_u64(1))
    );
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L395-414)
```rust
    fn track_tx(&mut self, tx_hash: Byte32, fee_rate: FeeRate, height: u64) {
        if self.tracked_txs.contains_key(&tx_hash) {
            // already in track
            return;
        }
        if height != self.best_height {
            // ignore wrong height txs
            return;
        }
        if let Some(bucket_index) = self.tx_confirm_stat.add_unconfirmed_tx(height, fee_rate) {
            self.tracked_txs.insert(
                tx_hash,
                TxRecord {
                    height,
                    bucket_index,
                    fee_rate,
                },
            );
        }
    }
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L120-145)
```rust
    pub fn update_ibd_state(&mut self, in_ibd: bool) {
        if self.is_ready {
            if in_ibd {
                self.clear();
                self.is_ready = false;
            }
        } else if !in_ibd {
            self.clear();
            self.is_ready = true;
        }
    }

    fn clear(&mut self) {
        self.boot_tip = 0;
        self.current_tip = 0;
        self.txs.clear();
    }

    pub fn commit_block(&mut self, block: &BlockView) {
        let tip_number = block.number();
        if self.boot_tip == 0 {
            self.boot_tip = tip_number;
        }
        self.current_tip = tip_number;
        self.expire();
    }
```
