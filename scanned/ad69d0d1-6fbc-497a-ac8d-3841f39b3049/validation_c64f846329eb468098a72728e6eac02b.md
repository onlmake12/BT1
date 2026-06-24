Audit Report

## Title
`FeeRateCollector::statistics()` Unconditional `.expect()` on `txs_sizes` Panics for Pre-`BlockExtV1` Canonical Blocks — (`File: rpc/src/util/fee_rate.rs`)

## Summary

`FeeRateCollector::statistics()` calls `.expect()` on `BlockExt.txs_sizes` without any guard on whether the field is populated. Blocks stored before `BlockExtV1` was introduced (v0.106) are deserialized with `txs_sizes: None`, and any such block appearing in the last `target` (up to 101) canonical-chain blocks causes an unconditional panic in the RPC handler. Any unprivileged caller of `get_fee_rate_statistics` or `get_fee_rate_statics` can trigger this.

## Finding Description

**Root cause — unconditional `.expect()` at line 93:**

In `rpc/src/util/fee_rate.rs`, the `statistics()` closure destructures `BlockExt` and immediately panics if `txs_sizes` is `None`:

```rust
let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
``` [1](#0-0) 

**How `txs_sizes: None` reaches the canonical chain:**

`get_block_ext` in the store reads raw bytes and branches on the number of extra fields:
- `count_extra_fields() == 0` → old `BlockExt` schema (pre-v0.106, 5 fields) → deserialized with `txs_sizes: None` and `cycles: None`
- `count_extra_fields() == 2` → `BlockExtV1` schema → `txs_sizes` taken from stored value (may still be `None`) [2](#0-1) 

The old-format deserialization path explicitly sets `txs_sizes: None`: [3](#0-2) 

**`FeeRateProvider::collect` performs no `verified` or `txs_sizes` guard:**

```rust
let block_ext_iter =
    (start..=tip_number).filter_map(|number| self.get_block_ext_by_number(number));
block_ext_iter.fold(Vec::new(), f)
``` [4](#0-3) 

`get_block_ext_by_number` returns any stored `BlockExt` regardless of `verified` status or whether `txs_sizes` is populated: [5](#0-4) 

**`insert_ok_ext` accepts `txs_sizes: None`:**

`insert_ok_ext` takes `txs_sizes: Option<Vec<u64>>` and assigns it directly, so a verified block can legitimately have `txs_sizes: None` stored: [6](#0-5) 

**The test suite confirms no validity gate exists:**

The unit tests construct `BlockExt` with `verified: None` and pass it directly through `FeeRateCollector`, but always supply `txs_sizes: Some(...)`. No test exercises the `txs_sizes: None` path, confirming the absence of a guard: [7](#0-6) 

**The store's own test stores `verified: Some(true)` with `txs_sizes: None`:** [8](#0-7) 

## Impact Explanation

A panic inside the `statistics()` closure propagates through `Iterator::fold` and out of the RPC handler. Depending on whether the JSON-RPC server wraps handlers in `catch_unwind`, this either kills the serving thread (making the RPC subsystem unresponsive) or returns an internal error. This matches the allowed impact: **Note (0–500 points) — Any local RPC API crash**.

## Likelihood Explanation

The condition is triggered when any block in the last `target` (up to 101) canonical-chain blocks has `txs_sizes: None`. This occurs on any node whose canonical chain includes blocks stored in the pre-v0.106 `BlockExt` format (5-field schema), which is the case for nodes that were operational before v0.106 and whose chain tip is within 101 blocks of the schema transition point, or for any node where `insert_ok_ext` was called with `txs_sizes: None`. A single unprivileged RPC call to `get_fee_rate_statistics` or `get_fee_rate_statics` is sufficient to trigger the panic.

## Recommendation

In the `statistics()` closure, replace the unconditional `.expect()` with a graceful skip:

```rust
let txs_sizes = match txs_sizes {
    Some(s) => s,
    None => return fee_rates, // skip blocks without size data
};
```

Optionally, also skip blocks where `block_ext.verified != Some(true)` to avoid consuming unverified data. This mirrors the pattern already used elsewhere in the codebase where optional fields are handled with `if let Some(...)` before use.

## Proof of Concept

1. Run a CKB node that was operational before v0.106 (when `BlockExtV1` was introduced), so that some blocks in the canonical chain are stored in the old 5-field `BlockExt` format.
2. Ensure the chain tip is within 101 blocks of the schema transition point (or use a short test chain).
3. Call `get_fee_rate_statistics` (or the deprecated `get_fee_rate_statics`) via JSON-RPC with no parameters.
4. `FeeRateProvider::collect` fetches those old `BlockExt` records via `get_block_ext_by_number`; `get_block_ext` deserializes them with `txs_sizes: None`.
5. `statistics()` reaches `txs_sizes.expect("expect txs_size's length >= 1")` with `txs_sizes = None` and panics.

Alternatively, write a unit test using `DummyFeeRateProvider` that appends a `BlockExt` with `txs_sizes: None` and calls `FeeRateCollector::new(&provider).statistics(None)` — this will panic immediately, reproducing the bug without any node setup.

### Citations

**File:** rpc/src/util/fee_rate.rs (L45-47)
```rust
        let block_ext_iter =
            (start..=tip_number).filter_map(|number| self.get_block_ext_by_number(number));
        block_ext_iter.fold(Vec::new(), f)
```

**File:** rpc/src/util/fee_rate.rs (L56-59)
```rust
    fn get_block_ext_by_number(&self, number: BlockNumber) -> Option<BlockExt> {
        self.get_block_hash(number)
            .and_then(|hash| self.get_block_ext(&hash))
    }
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

**File:** store/src/store.rs (L247-263)
```rust
    fn get_block_ext(&self, block_hash: &packed::Byte32) -> Option<BlockExt> {
        self.get(COLUMN_BLOCK_EXT, block_hash.as_slice())
            .map(|slice| {
                let reader =
                    packed::BlockExtReader::from_compatible_slice_should_be_ok(slice.as_ref());
                match reader.count_extra_fields() {
                    0 => reader.into(),
                    2 => packed::BlockExtV1Reader::from_slice_should_be_ok(slice.as_ref()).into(),
                    _ => {
                        panic!(
                            "BlockExt storage field count doesn't match, expect 7 or 5, actual {}",
                            reader.field_count()
                        )
                    }
                }
            })
    }
```

**File:** util/types/src/conversion/storage.rs (L139-151)
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
}
```

**File:** chain/src/verify.rs (L758-777)
```rust
    fn insert_ok_ext(
        &self,
        txn: &StoreTransaction,
        hash: &Byte32,
        mut ext: BlockExt,
        cache_entries: Option<&[Completed]>,
        txs_sizes: Option<Vec<u64>>,
    ) -> Result<(), Error> {
        ext.verified = Some(true);
        if let Some(entries) = cache_entries {
            let (txs_fees, cycles) = entries
                .iter()
                .map(|entry| (entry.fee, entry.cycles))
                .unzip();
            ext.txs_fees = txs_fees;
            ext.cycles = Some(cycles);
        }
        ext.txs_sizes = txs_sizes;
        txn.insert_block_ext(hash, &ext)
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

**File:** store/src/tests/db.rs (L54-62)
```rust
    let ext = BlockExt {
        received_at: block.timestamp(),
        total_difficulty: block.difficulty(),
        total_uncles_count: block.data().uncles().len() as u64,
        verified: Some(true),
        txs_fees: vec![],
        cycles: None,
        txs_sizes: None,
    };
```
