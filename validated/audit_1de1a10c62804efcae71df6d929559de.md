Audit Report

## Title
`FeeRateCollector::statistics()` Unconditional `.expect()` on `txs_sizes` Panics for Pre-`BlockExtV1` Blocks — (File: rpc/src/util/fee_rate.rs)

## Summary

`FeeRateCollector::statistics()` unconditionally calls `.expect()` on `block_ext.txs_sizes` at line 93. For any block stored in the old 5-field `BlockExt` format (pre-`BlockExtV1`), `get_block_ext` deserializes via `packed::BlockExtReader`, which hard-codes `txs_sizes: None`. Any caller of `get_fee_rate_statistics` with a target window covering such a block triggers a panic in the RPC handler.

## Finding Description

`get_block_ext` in `store/src/store.rs` (L252–253) branches on `count_extra_fields()`: when the stored record has 0 extra fields (old `BlockExt`), it uses `packed::BlockExtReader`, whose `From` and `Unpack` impls in `util/types/src/conversion/storage.rs` (L147–148, L162–163) hard-code `cycles: None, txs_sizes: None`. When the record has 2 extra fields (`BlockExtV1`), it uses `packed::BlockExtV1Reader`, which populates both fields.

`FeeRateProvider::collect` in `rpc/src/util/fee_rate.rs` (L45–46) iterates canonical-chain block numbers and calls `get_block_ext_by_number`, filtering only on whether the `BlockExt` itself is `Some` — no check on `txs_sizes`. The fold closure in `statistics()` (L86–111) then unconditionally calls:

```rust
let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
```

at L93. If any block in the fee-rate window was stored before `BlockExtV1` was introduced, `txs_sizes` is `None` and this line panics.

The unit tests in `rpc/src/tests/fee_rate.rs` (L47–116) always supply `txs_sizes: Some(...)` in every `BlockExt` entry, so the `None` branch is never exercised and provides no regression coverage.

## Impact Explanation

The panic propagates out of `statistics()` through `Iterator::fold`. This crashes the RPC handler for `get_fee_rate_statistics` (and `estimate_fee_rate`). This matches the allowed CKB bounty impact: **Any local RPC API crash (Note, 0–500 points)**.

## Likelihood Explanation

The condition is met on any CKB node that was operational before `BlockExtV1` was introduced and whose database has not been fully migrated. A single unprivileged RPC call to `get_fee_rate_statistics` with a `target` window large enough to include an old-format block is sufficient. No keys, privileges, or special network conditions are required.

## Recommendation

In the fold closure inside `statistics()`, replace the unconditional `.expect()` at L93 with a graceful skip:

```rust
let txs_sizes = match txs_sizes {
    Some(s) if !s.is_empty() => s,
    _ => return fee_rates,
};
```

Optionally, also filter out blocks where `block_ext.verified != Some(true)` in `FeeRateProvider::collect` to prevent unverified or failure-ext blocks from entering the window.

## Proof of Concept

1. Run a CKB node that was active before `BlockExtV1` was introduced, so that some canonical-chain blocks have `BlockExt` stored in the old 5-field format.
2. Call `get_fee_rate_statistics` via RPC with a `target` window that includes one of those old-format blocks.
3. `FeeRateProvider::collect` fetches those `BlockExt` records via `get_block_ext_by_number`; `get_block_ext` returns them with `txs_sizes: None` via the `count_extra_fields() == 0` branch in `store/src/store.rs` L252–253.
4. `statistics()` reaches `txs_sizes.expect("expect txs_size's length >= 1")` with `txs_sizes = None` and panics.

To reproduce in a unit test: in `rpc/src/tests/fee_rate.rs`, insert one `BlockExt` entry into `DummyFeeRateProvider` with `txs_sizes: None` and call `FeeRateCollector::new(&provider).statistics(None)` — the panic is immediate. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rpc/src/util/fee_rate.rs (L93-93)
```rust
            let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
```

**File:** store/src/store.rs (L250-254)
```rust
                let reader =
                    packed::BlockExtReader::from_compatible_slice_should_be_ok(slice.as_ref());
                match reader.count_extra_fields() {
                    0 => reader.into(),
                    2 => packed::BlockExtV1Reader::from_slice_should_be_ok(slice.as_ref()).into(),
```

**File:** util/types/src/conversion/storage.rs (L154-165)
```rust
impl<'r> From<packed::BlockExtReader<'r>> for core::BlockExt {
    fn from(value: packed::BlockExtReader<'r>) -> core::BlockExt {
        core::BlockExt {
            received_at: value.received_at().into(),
            total_difficulty: value.total_difficulty().into(),
            total_uncles_count: value.total_uncles_count().into(),
            verified: value.verified().into(),
            txs_fees: value.txs_fees().into(),
            cycles: None,
            txs_sizes: None,
        }
    }
```

**File:** rpc/src/tests/fee_rate.rs (L47-76)
```rust
#[test]
fn test_fee_rate_statics() {
    let mut provider = DummyFeeRateProvider::new(30);
    for i in 0..=21 {
        let ext = BlockExt {
            received_at: 0,
            total_difficulty: 0u64.into(),
            total_uncles_count: 0,
            verified: None,

            // txs_fees length is equal to block_ext.cycles length
            // and txs_fees does not include cellbase
            txs_fees: vec![Capacity::shannons(i * i * 100)],
            // cycles does not include cellbase
            cycles: Some(vec![i * 100]),
            // txs_sizes length is equal to block_ext.txs_fees length + 1
            // first element in txs_sizes is belong to cellbase
            txs_sizes: Some(vec![i * 5678, i * 100]),
        };
        provider.append(i, ext);
    }

    let statistics = FeeRateCollector::new(&provider).statistics(None);
    assert_eq!(
        statistics,
        Some(FeeRateStatistics {
            mean: 11_000.into(),
            median: 11_000.into(),
        })
    );
```
