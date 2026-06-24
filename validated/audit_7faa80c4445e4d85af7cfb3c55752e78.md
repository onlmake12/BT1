Audit Report

## Title
Unconditional `expect()` on `txs_sizes` in `FeeRateCollector::statistics` panics on pre-v0.106 `BlockExt` records — (`rpc/src/util/fee_rate.rs`)

## Summary
The `From<packed::BlockExtReader>` conversion for old 5-field `BlockExt` records hard-codes `txs_sizes: None`. The `statistics` method in `FeeRateCollector` calls `.expect("expect txs_size's length >= 1")` on this field unconditionally, with no guard. Any node that was upgraded in-place from before v0.106 and retains old-format `COLUMN_BLOCK_EXT` entries will panic when `get_fee_rate_statistics` is called and the iteration window covers those blocks.

## Finding Description
**Deserialization yields `txs_sizes: None` for old records.**
`get_block_ext` in `store/src/store.rs` dispatches on `count_extra_fields()`: a value of `0` (old 5-field format) calls `reader.into()`, which resolves to `From<packed::BlockExtReader>`. That impl hard-codes `txs_sizes: None`. [1](#0-0) [2](#0-1) 

**`collect()` does not filter `txs_sizes: None`.**
The `filter_map` in `collect` only drops blocks for which `get_block_ext_by_number` returns `None` (i.e., missing block hash). Old-format blocks return `Some(BlockExt { txs_sizes: None, … })` and are passed directly to the closure. [3](#0-2) 

**Unconditional `expect` fires on every old-format block.**
Inside the closure passed to `collect`, there is no `if let Some`, no early return, and no skip — just a bare `.expect(…)`. [4](#0-3) 

**No `catch_unwind` in the RPC layer.**
A search of `rpc/**/*.rs` finds zero uses of `catch_unwind` or equivalent panic guards. The panic propagates through the tokio task. Under standard tokio behavior the task is aborted and the request returns an error; the node process itself survives. The RPC endpoint is left in a state where every call to `get_fee_rate_statistics` or `get_fee_rate_statics` panics until the old-format blocks scroll out of the 101-block window.

**Test suite never exercises `txs_sizes: None`.**
Every entry in the existing test fixture sets `txs_sizes: Some(…)`, so this path has no test coverage. [5](#0-4) 

## Impact Explanation
The panic is contained within the tokio task; the node process does not terminate. The concrete impact is that the `get_fee_rate_statistics` RPC method crashes on every call while old-format blocks are in the iteration window, returning an internal error to the caller. This matches **Note (0 – 500 points): Any local RPC API crash**. The higher severity claimed in the submission (node termination) is not supported by standard tokio task-panic semantics and no evidence of a non-default runtime configuration is provided.

## Likelihood Explanation
Any mainnet or testnet node upgraded in-place from before v0.106 without a full re-sync retains old-format `BlockExt` records. The `target` window is capped at 101 blocks, so the condition persists until the chain tip advances more than 101 blocks past the last old-format record. No authentication, key, or peer relationship is required — a single unauthenticated HTTP POST to the public JSON-RPC port is sufficient to trigger the panic repeatedly.

## Recommendation
Replace the unconditional `expect` with a graceful skip for blocks that lack `txs_sizes`:

```rust
let Some(txs_sizes) = txs_sizes else {
    return fee_rates; // skip pre-v0.106 blocks silently
};
```

Alternatively, filter in `collect()` by changing `filter_map` to also drop `BlockExt` entries where `txs_sizes.is_none()`, or add a startup migration that back-fills `txs_sizes` for old blocks. [6](#0-5) 

## Proof of Concept
1. Start a CKB node that has `COLUMN_BLOCK_EXT` entries with 5-field `BlockExt` (any node upgraded from before v0.106 without re-sync).
2. Send:
   ```json
   {"jsonrpc":"2.0","method":"get_fee_rate_statistics","params":[{"value":"0x65"}],"id":1}
   ```
3. `collect(101, …)` iterates the last 101 blocks, hits the first old-format block, deserializes it to `core::BlockExt { txs_sizes: None, … }`, and the closure fires `None.expect("expect txs_size's length >= 1")` → panic, task abort, RPC error response.

Unit-test reproduction: add a `BlockExt { txs_sizes: None, … }` entry to `DummyFeeRateProvider` in `rpc/src/tests/fee_rate.rs` and call `FeeRateCollector::new(&provider).statistics(None)` — it panics immediately, confirming the existing test suite never covers this case. [7](#0-6)

### Citations

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

**File:** store/src/store.rs (L252-261)
```rust
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
```

**File:** rpc/src/util/fee_rate.rs (L45-47)
```rust
        let block_ext_iter =
            (start..=tip_number).filter_map(|number| self.get_block_ext_by_number(number));
        block_ext_iter.fold(Vec::new(), f)
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

**File:** rpc/src/tests/fee_rate.rs (L47-116)
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

    let statistics = FeeRateCollector::new(&provider).statistics(Some(9));
    assert_eq!(
        statistics,
        Some(FeeRateStatistics {
            mean: 17_000.into(),
            median: 17_000.into()
        })
    );

    let statistics = FeeRateCollector::new(&provider).statistics(Some(30));
    assert_eq!(
        statistics,
        Some(FeeRateStatistics {
            mean: 11_000.into(),
            median: 11_000.into(),
        })
    );

    let statistics = FeeRateCollector::new(&provider).statistics(Some(0));
    assert_eq!(
        statistics,
        Some(FeeRateStatistics {
            mean: 21_000.into(),
            median: 21_000.into(),
        })
    );

    provider.set_max_target(10);
    let statistics11 = FeeRateCollector::new(&provider).statistics(Some(11));
    let statistics12 = FeeRateCollector::new(&provider).statistics(Some(12));
    assert_eq!(statistics11, statistics12);
    assert_eq!(
        statistics11,
        Some(FeeRateStatistics {
            mean: 16500.into(),
            median: 16500.into(),
        })
    );
}
```
