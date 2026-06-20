### Title
Unsafe Arithmetic Overflow in Fee Estimator Bucket Index Calculation — (`util/fee-estimator/src/estimator/weight_units_flow.rs`)

---

### Summary

The `weight_units_flow` fee estimator performs raw, unchecked `u64` arithmetic in `max_bucket_index_by_fee_rate` and `do_estimate`. When a transaction with an extreme fee rate (achievable by any unprivileged tx-pool submitter) is present in the mempool, the addition `x + t * 11_500` in the final match arm of `max_bucket_index_by_fee_rate` overflows `u64`, producing a drastically wrong (wrapped) bucket index. This propagates into `lowest_fee_rate_by_bucket_index` and the weight comparison in `do_estimate`, causing the `estimate_fee_rate` RPC to return a severely underestimated fee rate. If the node is compiled with `overflow-checks = true`, the same path causes a panic, making the fee-estimator RPC unavailable.

---

### Finding Description

In `util/fee-estimator/src/estimator/weight_units_flow.rs`, the function `max_bucket_index_by_fee_rate` maps a `FeeRate` (a plain `u64`) to a bucket index using raw integer arithmetic:

```rust
// line 349-359
fn max_bucket_index_by_fee_rate(fee_rate: FeeRate) -> usize {
    let t = FEE_RATE_UNIT;   // = 1_000  (u64)
    let index = match fee_rate.as_u64() {
        ...
        x => (x + t * 11_500) / (100 * t),   // line 357 — raw u64 addition
    };
    index as usize
}
```

`t * 11_500 = 11_500_000`. For any `fee_rate.as_u64()` value greater than `u64::MAX - 11_500_000` (≈ `18_446_744_073_698_051_615`), the addition `x + 11_500_000` silently wraps in release mode, producing a tiny index (e.g., `~114`) instead of the astronomically large correct value.

The fee rate of a mempool entry is computed in `util/types/src/core/fee_rate.rs`:

```rust
// line 15
FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)
```

`saturating_mul` caps the intermediate product at `u64::MAX`, so a transaction with a large fee and weight = 1 produces `fee_rate = u64::MAX`. This value is stored in the estimator's internal `txs` map via `accept_tx` and later retrieved as `max_fee_rate` in `do_estimate`.

The corrupted bucket index then flows into `lowest_fee_rate_by_bucket_index` (lines 325–344), which also performs raw multiplications:

```rust
// line 343
x => t * (10 + 20 * 2 + 30 * 5 + 30 * 10 + 25 * 20 + 20 * 50 + (x - 135) * 100),
```

For a legitimately large (non-wrapped) index, `(x - 135) * 100` itself overflows. For the wrapped small index produced by the first overflow, the wrong (too-low) fee rate tier is selected.

Additionally, `do_estimate` contains two more raw multiplications that are unsafe if `target_blocks` is large:

```rust
// line 281
let added_weight = flow_speed_buckets[bucket_index] * target_blocks;
// line 284
let removed_weight = (MAX_BLOCK_BYTES * 85 / 100) * target_blocks;
```

Both are plain `u64 * u64` with no overflow guard. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

---

### Impact Explanation

The `estimate_fee_rate` RPC (exposed via `rpc/src/module/experiment.rs`) returns a fee rate that users and wallets rely on to set transaction fees. When the overflow is triggered:

- **Release mode (wrapping):** The returned fee rate is severely underestimated (e.g., `1_480_000` shannons/KW instead of the correct very-high value). Users following this estimate set fees too low, causing their transactions to stall or be evicted from the mempool.
- **Debug / overflow-checked mode:** The node panics inside the RPC handler, making `estimate_fee_rate` permanently unavailable until the node is restarted and the offending transaction is no longer tracked. [5](#0-4) 

---

### Likelihood Explanation

Any unprivileged tx-pool submitter can craft a transaction with a very large fee (close to `u64::MAX` shannons) and a minimal serialized weight (e.g., weight = 1). `FeeRate::calculate` uses `saturating_mul`, so the resulting stored fee rate is exactly `u64::MAX`. The estimator's `accept_tx` path stores this value unconditionally. The next call to `estimate_fee_rate` by any RPC caller then triggers the overflow. No special privilege, key, or majority hash power is required. [6](#0-5) [4](#0-3) 

---

### Recommendation

Replace all raw `u64` arithmetic in `max_bucket_index_by_fee_rate`, `lowest_fee_rate_by_bucket_index`, and `do_estimate` with checked or saturating variants:

- Use `x.checked_add(t * 11_500).unwrap_or(u64::MAX)` (or `saturating_add`) in `max_bucket_index_by_fee_rate`.
- Use `x.saturating_sub(135).saturating_mul(100)` and `t.saturating_mul(...)` in `lowest_fee_rate_by_bucket_index`.
- Use `flow_speed_buckets[bucket_index].saturating_mul(target_blocks)` and `(MAX_BLOCK_BYTES * 85 / 100).saturating_mul(target_blocks)` in `do_estimate`.

Cap the bucket index at a defined maximum (e.g., `usize::MAX / 2`) before passing it to `lowest_fee_rate_by_bucket_index`. [1](#0-0) 

---

### Proof of Concept

1. Construct a CKB transaction with `fee ≈ u64::MAX / 1000` shannons and serialized weight = 1 byte.
2. Submit it to the node's tx-pool via `send_transaction` RPC. `FeeRate::calculate` stores `fee_rate = u64::MAX` in the estimator.
3. Call `estimate_fee_rate` RPC.
4. Inside `do_estimate`, `max_fee_rate = u64::MAX` is passed to `max_bucket_index_by_fee_rate`.
5. The last match arm executes `u64::MAX + 11_500_000`, which wraps to `11_499_999` in release mode.
6. `11_499_999 / 100_000 = 114` is used as the bucket index.
7. `lowest_fee_rate_by_bucket_index(114)` returns `1_480_000` shannons/KW — a fraction of the correct value.
8. The RPC returns this wrong estimate to all callers. [7](#0-6) [2](#0-1)

### Citations

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L153-162)
```rust
    pub fn accept_tx(&mut self, info: TxEntryInfo) {
        if self.current_tip == 0 {
            return;
        }
        let item = TxStatus::new_from_entry_info(info);
        self.txs
            .entry(self.current_tip)
            .and_modify(|items| items.push(item))
            .or_insert_with(|| vec![item]);
    }
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L188-215)
```rust
impl Algorithm {
    fn do_estimate(
        &self,
        target_blocks: BlockNumber,
        sorted_current_txs: &[TxStatus],
    ) -> Result<FeeRate, Error> {
        ckb_logger::debug!(
            "boot: {}, current: {}, target: {target_blocks} blocks",
            self.boot_tip,
            self.current_tip,
        );
        let historical_blocks = Self::historical_blocks(target_blocks);
        ckb_logger::debug!("required: {historical_blocks} blocks");
        if historical_blocks > self.current_tip.saturating_sub(self.boot_tip) {
            return Err(Error::LackData);
        }

        let max_fee_rate = if let Some(fee_rate) = sorted_current_txs.first().map(|tx| tx.fee_rate)
        {
            fee_rate
        } else {
            return Ok(constants::LOWEST_FEE_RATE);
        };

        ckb_logger::debug!("max fee rate of current transactions: {max_fee_rate}");

        let max_bucket_index = Self::max_bucket_index_by_fee_rate(max_fee_rate);
        ckb_logger::debug!("current weight buckets size: {}", max_bucket_index + 1);
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L279-285)
```rust
        for bucket_index in 1..=max_bucket_index {
            let current_weight = current_weight_buckets[bucket_index];
            let added_weight = flow_speed_buckets[bucket_index] * target_blocks;
            // Note: blocks are not full even there are many pending transactions,
            // since `MAX_BLOCK_PROPOSALS_LIMIT = 1500`.
            let removed_weight = (MAX_BLOCK_BYTES * 85 / 100) * target_blocks;
            let passed = current_weight + added_weight <= removed_weight;
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L325-344)
```rust
    fn lowest_fee_rate_by_bucket_index(index: usize) -> FeeRate {
        let t = FEE_RATE_UNIT;
        let value = match index as u64 {
            // 0->0
            0 => 0,
            // 1->1000, 2->2000, .., 10->10000
            x if x <= 10 => t * x,
            // 11->12000, 12->14000, .., 30->50000
            x if x <= 30 => t * (10 + (x - 10) * 2),
            // 31->55000, 32->60000, ..., 60->200000
            x if x <= 60 => t * (10 + 20 * 2 + (x - 30) * 5),
            // 61->210000, 62->220000, ..., 90->500000
            x if x <= 90 => t * (10 + 20 * 2 + 30 * 5 + (x - 60) * 10),
            // 91->520000, 92->540000, ..., 115 -> 1000000
            x if x <= 115 => t * (10 + 20 * 2 + 30 * 5 + 30 * 10 + (x - 90) * 20),
            // 116->1050000, 117->1100000, ..., 135->2000000
            x if x <= 135 => t * (10 + 20 * 2 + 30 * 5 + 30 * 10 + 25 * 20 + (x - 115) * 50),
            // 136->2100000,  137->2200000, ...
            x => t * (10 + 20 * 2 + 30 * 5 + 30 * 10 + 25 * 20 + 20 * 50 + (x - 135) * 100),
        };
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L348-360)
```rust
    fn max_bucket_index_by_fee_rate(fee_rate: FeeRate) -> usize {
        let t = FEE_RATE_UNIT;
        let index = match fee_rate.as_u64() {
            x if x <= 10_000 => x / t,
            x if x <= 50_000 => (x + t * 10) / (2 * t),
            x if x <= 200_000 => (x + t * 100) / (5 * t),
            x if x <= 500_000 => (x + t * 400) / (10 * t),
            x if x <= 1_000_000 => (x + t * 1_300) / (20 * t),
            x if x <= 2_000_000 => (x + t * 4_750) / (50 * t),
            x => (x + t * 11_500) / (100 * t),
        };
        index as usize
    }
```

**File:** util/types/src/core/fee_rate.rs (L11-16)
```rust
    pub fn calculate(fee: Capacity, weight: u64) -> Self {
        if weight == 0 {
            return FeeRate::zero();
        }
        FeeRate::from_u64(fee.as_u64().saturating_mul(KW) / weight)
    }
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
