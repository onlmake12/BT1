Audit Report

## Title
Unconditional `.expect()` on `txs_sizes` in `FeeRateCollector::statistics` panics on pre-migration `BlockExt` — (`rpc/src/util/fee_rate.rs`)

## Summary

`FeeRateCollector::statistics` calls `.expect()` unconditionally on `block_ext.txs_sizes` at line 93 of `rpc/src/util/fee_rate.rs`. Legacy `BlockExt` records (stored before `BlockExtV1`) always deserialize with `txs_sizes: None`. Calling `get_fee_rate_statistics(0)` on a node whose tip block was stored in the legacy format deterministically triggers this panic via the public, unauthenticated RPC endpoint.

## Finding Description

**Arithmetic trace for `target = 0`:**

In `statistics`, the target is adjusted:
- `target = 0` → `is_even(0)` is true → `target = 0u64.saturating_add(1) = 1`
- `min(MAX_TARGET=101, 1) = 1`

`collect(1, …)` then computes:
- `start = max(MIN_TARGET=1, tip+1−1) = max(1, tip) = tip` (for any tip ≥ 1)
- Iteration range: `[tip, tip]` — exactly one block, the tip.

The closure immediately hits the unconditional `.expect()`:

```rust
// rpc/src/util/fee_rate.rs, line 93
let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
```

**Root cause — legacy deserialization hardcodes `txs_sizes: None`:**

The `packed::BlockExtReader` `Unpack` impl (the non-V1 path) always produces `txs_sizes: None`:

```rust
// util/types/src/conversion/storage.rs, lines 139–150
impl<'r> Unpack<core::BlockExt> for packed::BlockExtReader<'r> {
    fn unpack(&self) -> core::BlockExt {
        core::BlockExt {
            ...
            cycles: None,
            txs_sizes: None,   // always None for pre-migration blocks
        }
    }
}
```

There is no migration that back-fills `txs_sizes` for old blocks. The `filter_map` in `collect` only skips blocks where the block ext is entirely absent from the store; it does not skip blocks where `txs_sizes` is `None`.

**Existing guards are insufficient:**

The only guard before the `.expect()` is the `filter_map` that drops blocks with no `BlockExt` at all. Once a `BlockExt` is returned (even a legacy one), the closure runs unconditionally and panics on `None`.

## Impact Explanation

A panic in the RPC handler thread crashes that request handler. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash.** The claim's attempt to elevate this to High (node process crash) is not concretely proven; Tokio-based RPC servers typically catch panics at task boundaries, limiting the blast radius to the individual request. The confirmed, reproducible impact is an RPC API crash.

## Likelihood Explanation

The precondition — tip block has `txs_sizes = None` — is met on any node that:
1. Has not yet synced past the `BlockExtV1` migration boundary, or
2. Was started with `switch.disable_all()` (fast-sync / assume-valid mode), or
3. Has a genesis or very early block as its tip.

The trigger is a single unauthenticated RPC call (`get_fee_rate_statistics` with `target=0`). No special privileges are required. The existing test suite for `target=0` only exercises providers where all blocks have `txs_sizes: Some(…)`, leaving the panic path untested.

## Recommendation

Replace the unconditional `.expect()` with a graceful skip, consistent with the existing pattern used for `cycles`:

```rust
// rpc/src/util/fee_rate.rs, inside the collect closure
let Some(txs_sizes) = txs_sizes else {
    return fee_rates; // skip pre-migration blocks without size data
};
```

Add a test case using a `DummyFeeRateProvider` with `txs_sizes: None` to cover this path.

## Proof of Concept

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
// target=0 → adjusted to 1 → fetches only block at tip (block 1) → .expect() panics
let _ = FeeRateCollector::new(&provider).statistics(Some(0));
```