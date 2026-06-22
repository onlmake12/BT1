### Title
Unconditional `.expect()` on `txs_sizes` in `FeeRateCollector::statistics` panics on pre-migration `BlockExt` — (`rpc/src/util/fee_rate.rs`)

### Summary

`FeeRateCollector::statistics` calls `.expect()` unconditionally on `block_ext.txs_sizes`, which is `None` for any block stored in the legacy `BlockExt` wire format. An unprivileged caller sending `get_fee_rate_statistics(0)` narrows the scan to exactly the tip block; if that block was stored pre-migration, the panic fires deterministically.

### Finding Description

**Arithmetic trace for `target = 0`:**

In `rpc/src/util/fee_rate.rs`, `statistics` adjusts the target: [1](#0-0) 

- `target = 0` → `is_even(0)` is true → `target = 0u64.saturating_add(1) = 1`
- `min(MAX_TARGET=101, 1) = 1`

`collect(1, …)` then computes: [2](#0-1) 

- `start = max(MIN_TARGET=1, tip+1−1) = max(1, tip) = tip` (for any tip ≥ 1)
- Iteration range: `[tip, tip]` — exactly one block, the tip.

The closure then hits the unconditional `.expect()`: [3](#0-2) 

**Why `txs_sizes = None` is a structural guarantee for pre-migration blocks:**

The legacy `BlockExt` (non-V1) deserialization in `util/types/src/conversion/storage.rs` hardcodes `txs_sizes: None` for every block stored in the old format: [4](#0-3) 

There is no migration that back-fills `txs_sizes` for old blocks. Any node whose tip block was stored before `BlockExtV1` was introduced will return a `BlockExt` with `txs_sizes = None` from `get_block_ext_by_number`.

Additionally, the `switch.disable_all()` fast-path in block verification explicitly passes `None` for `txs_sizes`: [5](#0-4) 

The `collect` function's `filter_map` only skips blocks where the block ext is entirely absent; it does **not** skip blocks where `txs_sizes = None`: [6](#0-5) 

### Impact Explanation

A panic in the RPC handler thread crashes that request handler. Depending on whether the RPC server catches panics at the task boundary, this can range from a single failed RPC response to a full node process crash. The `get_fee_rate_statistics` RPC is publicly accessible with no authentication requirement: [7](#0-6) 

### Likelihood Explanation

The precondition — tip block has `txs_sizes = None` — is met on any node that:
1. Has not yet synced past the `BlockExtV1` migration boundary, or
2. Was started with `switch.disable_all()` (e.g., fast-sync / assume-valid mode), or
3. Has a genesis or early block as its tip (genesis `BlockExt` is always stored with `txs_sizes: None` per the store test).

The existing test suite for `target=0` only exercises providers where all blocks have `txs_sizes: Some(…)`, so the panic path is untested: [8](#0-7) 

### Recommendation

Replace the `.expect()` with a graceful skip:

```rust
// rpc/src/util/fee_rate.rs, inside the collect closure
let Some(txs_sizes) = txs_sizes else {
    return fee_rates; // skip blocks without size data
};
```

This matches the existing pattern used for `cycles` (which is already wrapped in `if let Some(cycles) = cycles`). [9](#0-8) 

### Proof of Concept

```rust
// Unit test: FeeRateCollector::statistics(Some(0)) with txs_sizes = None panics
let mut provider = DummyFeeRateProvider::new(101);
provider.append(1, BlockExt {
    received_at: 0,
    total_difficulty: 0u64.into(),
    total_uncles_count: 0,
    verified: Some(true),
    txs_fees: vec![],
    cycles: None,
    txs_sizes: None,  // pre-migration block
});
// target=0 → 1 → fetches only block 1 → .expect() panics
let _ = FeeRateCollector::new(&provider).statistics(Some(0));
```

### Citations

**File:** rpc/src/util/fee_rate.rs (L39-47)
```rust
        let tip_number = self.get_tip_number();
        let start = std::cmp::max(
            MIN_TARGET,
            tip_number.saturating_add(1).saturating_sub(target),
        );

        let block_ext_iter =
            (start..=tip_number).filter_map(|number| self.get_block_ext_by_number(number));
        block_ext_iter.fold(Vec::new(), f)
```

**File:** rpc/src/util/fee_rate.rs (L79-84)
```rust
    pub fn statistics(&self, target: Option<u64>) -> Option<FeeRateStatistics> {
        let mut target = target.unwrap_or(DEFAULT_TARGET);
        if is_even(target) {
            target = target.saturating_add(1);
        }
        target = std::cmp::min(self.provider.max_target(), target);
```

**File:** rpc/src/util/fee_rate.rs (L86-93)
```rust
        let mut fee_rates = self.provider.collect(target, |mut fee_rates, block_ext| {
            let BlockExt {
                txs_sizes,
                cycles,
                txs_fees,
                ..
            } = block_ext;
            let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
```

**File:** rpc/src/util/fee_rate.rs (L97-109)
```rust
                if let Some(cycles) = cycles {
                    for (fee, cycles, size) in itertools::izip!(
                        txs_fees,
                        cycles,
                        txs_sizes.iter().skip(1) // skip cellbase (first element in the Vec)
                    ) {
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
                        }
                    }
                }
            }
```

**File:** util/types/src/conversion/storage.rs (L139-150)
```rust
impl<'r> Unpack<core::BlockExt> for packed::BlockExtReader<'r> {
    fn unpack(&self) -> core::BlockExt {
        core::BlockExt {
            received_at: self.received_at().into(),
            total_difficulty: self.total_difficulty().into(),
            total_uncles_count: self.total_uncles_count().into(),
            verified: self.verified().into(),
            txs_fees: self.txs_fees().into(),
            cycles: None,
            txs_sizes: None,
        }
    }
```

**File:** chain/src/verify.rs (L718-724)
```rust
            } else {
                txn.attach_block(b)?;
                attach_block_cell(&txn, b)?;
                mmr.push(b.digest())
                    .map_err(|e| InternalErrorKind::MMR.other(e))?;
                self.insert_ok_ext(&txn, &b.header().hash(), ext.clone(), None, None)?;
            }
```

**File:** rpc/src/module/chain.rs (L2129-2132)
```rust
    fn get_fee_rate_statistics(&self, target: Option<Uint64>) -> Result<Option<FeeRateStatistics>> {
        Ok(FeeRateCollector::new(self.shared.snapshot().as_ref())
            .statistics(target.map(Into::into)))
    }
```

**File:** rpc/src/tests/fee_rate.rs (L96-103)
```rust
    let statistics = FeeRateCollector::new(&provider).statistics(Some(0));
    assert_eq!(
        statistics,
        Some(FeeRateStatistics {
            mean: 21_000.into(),
            median: 21_000.into(),
        })
    );
```
