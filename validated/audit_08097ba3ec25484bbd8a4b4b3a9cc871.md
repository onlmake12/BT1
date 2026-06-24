Audit Report

## Title
Unconditional `.expect()` on `txs_sizes` in `FeeRateCollector::statistics` panics on pre-migration `BlockExt` — (`rpc/src/util/fee_rate.rs`)

## Summary

`FeeRateCollector::statistics` calls `.expect()` unconditionally on `block_ext.txs_sizes` at line 93 of `rpc/src/util/fee_rate.rs`. The legacy `BlockExt` wire format deserialization hardcodes `txs_sizes: None` for every pre-`BlockExtV1` block. When `get_fee_rate_statistics(0)` is called and the tip block was stored in the legacy format, the panic fires deterministically, crashing the RPC handler for that request.

## Finding Description

**Unconditional `.expect()` at the trigger site:**

In `rpc/src/util/fee_rate.rs` line 93, `txs_sizes` is unwrapped without any guard: [1](#0-0) 

**Arithmetic trace for `target = 0`:**

In `statistics`, `target = 0` is even, so it becomes `1`. `min(MAX_TARGET=101, 1) = 1`. In `collect(1, ...)`: [2](#0-1) 

`start = max(MIN_TARGET=1, tip+1−1) = tip` for any `tip ≥ 1`. The iteration range is `[tip, tip]` — exactly one block, the tip.

**Legacy deserialization hardcodes `txs_sizes: None`:**

The `packed::BlockExtReader` `Unpack` impl (for all pre-V1 blocks) always produces `txs_sizes: None`: [3](#0-2) 

There is no back-fill migration. Any block stored before `BlockExtV1` was introduced will return a `BlockExt` with `txs_sizes: None` from `get_block_ext_by_number`.

**`filter_map` does not skip `txs_sizes: None` blocks:**

The `collect` function's `filter_map` only skips blocks where the block ext is entirely absent (`None` from `get_block_ext_by_number`); it does not skip blocks where `txs_sizes` is `None` inside the returned `BlockExt`: [4](#0-3) 

**Existing tests do not cover the panic path:**

The test for `target=0` at line 96 uses a provider where all blocks have `txs_sizes: Some(...)`, so the panic path is never exercised: [5](#0-4) 

## Impact Explanation

A panic in the RPC handler crashes that request. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash.** The `get_fee_rate_statistics` RPC is publicly accessible with no authentication requirement: [6](#0-5) 

## Likelihood Explanation

The precondition — tip block has `txs_sizes: None` — is met on any node that has not yet synced past the `BlockExtV1` migration boundary, or was started with `switch.disable_all()` (fast-sync/assume-valid mode). An unprivileged caller can then send `get_fee_rate_statistics(0)` to trigger the panic deterministically. The attacker does not control the node's sync state, but the condition is realistic for partially-synced or assume-valid nodes.

## Recommendation

Replace the unconditional `.expect()` with a graceful skip, matching the existing pattern used for `cycles`:

```rust
// rpc/src/util/fee_rate.rs, line 93
let Some(txs_sizes) = txs_sizes else {
    return fee_rates;
};
``` [1](#0-0) 

## Proof of Concept

```rust
// Add to rpc/src/tests/fee_rate.rs
#[test]
#[should_panic(expected = "expect txs_size's length >= 1")]
fn test_fee_rate_statistics_panics_on_pre_migration_block() {
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
    // target=0 → adjusted to 1 → fetches only block 1 (tip) → .expect() panics
    let _ = FeeRateCollector::new(&provider).statistics(Some(0));
}
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

**File:** rpc/src/util/fee_rate.rs (L93-93)
```rust
            let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
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

**File:** rpc/src/module/chain.rs (L2129-2132)
```rust
    fn get_fee_rate_statistics(&self, target: Option<Uint64>) -> Result<Option<FeeRateStatistics>> {
        Ok(FeeRateCollector::new(self.shared.snapshot().as_ref())
            .statistics(target.map(Into::into)))
    }
```
