### Title
`FeeRateCollector::statistics()` Processes `BlockExt` Without Checking `verified` Field, Panicking on `txs_sizes: None` — (`File: rpc/src/util/fee_rate.rs`)

### Summary

`FeeRateCollector::statistics()` iterates recent canonical-chain blocks and unconditionally calls `.expect()` on the `txs_sizes` field of each `BlockExt` without first checking whether the block is verified (`BlockExt.verified == Some(true)`). Because `txs_sizes` is only populated by `insert_ok_ext` (which sets `verified = Some(true)`), any block whose `BlockExt` has `txs_sizes: None` — including blocks stored before `BlockExtV1` introduced the field, or blocks in a transient unverified state — causes a panic in the RPC handler. This is reachable by any unprivileged RPC caller.

### Finding Description

`BlockExt` carries a `verified: Option<bool>` field:

- `None` → stored but not yet verified
- `Some(false)` → verification failed
- `Some(true)` → fully verified [1](#0-0) 

`txs_sizes` (and `txs_fees`, `cycles`) are only written in `insert_ok_ext`, which simultaneously sets `verified = Some(true)`: [2](#0-1) 

`insert_failure_ext` sets `verified = Some(false)` but leaves `txs_fees`, `cycles`, and `txs_sizes` empty/`None`: [3](#0-2) 

`FeeRateProvider::collect` fetches `BlockExt` for the last N canonical-chain blocks via `get_block_ext_by_number`, filtering only on whether the result is `Some` — it does **not** filter on `verified`: [4](#0-3) 

The `Snapshot` implementation of `get_block_ext_by_number` returns any stored `BlockExt` regardless of `verified` status: [5](#0-4) 

Inside `statistics()`, the closure unconditionally calls `.expect()` on `txs_sizes`: [6](#0-5) 

If any block in the fee-rate window has `txs_sizes: None`, this `.expect()` panics. The test suite itself constructs `BlockExt` with `verified: None` and passes it through `FeeRateCollector`, confirming no validity gate exists: [7](#0-6) 

### Impact Explanation

A panic inside the RPC handler closure propagates through `Iterator::fold` and out of `statistics()`. Depending on whether the JSON-RPC server wraps handlers in `catch_unwind`, this either crashes the serving thread/task (causing the node's RPC subsystem to become unresponsive) or returns an internal error. Either outcome is a denial-of-service against the fee-rate estimation RPC, which downstream tooling (wallets, exchanges, fee bumping services) relies on for correct transaction fee selection. Incorrect fee rate data (if the panic is avoided but unverified data is included) would cause users to over- or under-pay fees.

### Likelihood Explanation

The condition is triggered whenever a block in the last `target` (up to 101) canonical-chain blocks has `txs_sizes: None`. This occurs on any node that:

1. Was running before `BlockExtV1` (with `txs_sizes`) was introduced and has not migrated those old `BlockExt` records, **or**
2. Has a block in a transient unverified state accessible via the canonical-chain number index.

Any unprivileged caller of the `get_fee_rate_statistics` (or `estimate_fee_rate`) RPC can trigger the panic with a single request. No special privileges, keys, or majority hash power are required.

### Recommendation

In `FeeRateCollector::statistics()` (and `FeeRateProvider::collect`), skip any `BlockExt` whose `verified` field is not `Some(true)` before destructuring its fields. Replace the unconditional `.expect()` on `txs_sizes` with a graceful `match` or `?`-style early return:

```rust
// In collect closure:
if block_ext.verified != Some(true) {
    return fee_rates; // skip unverified/invalid blocks
}
let txs_sizes = match block_ext.txs_sizes {
    Some(s) if s.len() >= 1 => s,
    _ => return fee_rates,
};
```

This mirrors the correct pattern used in `Market` (from the reference report) where the validity of the latest state is checked before its data is consumed.

### Proof of Concept

1. Run a CKB node that was operational before `BlockExtV1` (with `txs_sizes`) was introduced, so that some blocks in the canonical chain have `BlockExt.txs_sizes = None`.
2. Call the `get_fee_rate_statistics` RPC (or `estimate_fee_rate` in the experiment module).
3. `FeeRateProvider::collect` fetches those old `BlockExt` records via `get_block_ext_by_number`.
4. `statistics()` reaches `txs_sizes.expect("expect txs_size's length >= 1")` with `txs_sizes = None`.
5. The thread/task panics, crashing the RPC handler.

The root cause line is: [8](#0-7) 

with the missing validity guard being the absence of any check on `block_ext.verified` before this point.

### Citations

**File:** util/types/src/core/extras.rs (L22-41)
```rust
#[derive(Clone, PartialEq, Default, Debug, Eq)]
pub struct BlockExt {
    /// Timestamp when the block was received.
    pub received_at: u64,
    /// Total cumulative difficulty at this block.
    pub total_difficulty: U256,
    /// Total number of uncle blocks up to this block.
    pub total_uncles_count: u64,
    /// Whether the block has been verified.
    pub verified: Option<bool>,
    /// Transaction fees for each transaction except the cellbase.
    /// The length of `txs_fees` is equal to the length of `cycles`.
    pub txs_fees: Vec<Capacity>,
    /// Execution cycles for each transaction except the cellbase.
    /// The length of `cycles` is equal to the length of `txs_fees`.
    pub cycles: Option<Vec<Cycle>>,
    /// Sizes of each transaction including the cellbase.
    /// The length of `txs_sizes` is `txs_fees` length + 1.
    pub txs_sizes: Option<Vec<u64>>,
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

**File:** chain/src/verify.rs (L779-787)
```rust
    fn insert_failure_ext(
        &self,
        txn: &StoreTransaction,
        hash: &Byte32,
        mut ext: BlockExt,
    ) -> Result<(), Error> {
        ext.verified = Some(false);
        txn.insert_block_ext(hash, &ext)
    }
```

**File:** rpc/src/util/fee_rate.rs (L35-48)
```rust
    fn collect<F>(&self, target: u64, f: F) -> Vec<u64>
    where
        F: FnMut(Vec<u64>, BlockExt) -> Vec<u64>,
    {
        let tip_number = self.get_tip_number();
        let start = std::cmp::max(
            MIN_TARGET,
            tip_number.saturating_add(1).saturating_sub(target),
        );

        let block_ext_iter =
            (start..=tip_number).filter_map(|number| self.get_block_ext_by_number(number));
        block_ext_iter.fold(Vec::new(), f)
    }
```

**File:** rpc/src/util/fee_rate.rs (L51-64)
```rust
impl FeeRateProvider for Snapshot {
    fn get_tip_number(&self) -> BlockNumber {
        self.tip_number()
    }

    fn get_block_ext_by_number(&self, number: BlockNumber) -> Option<BlockExt> {
        self.get_block_hash(number)
            .and_then(|hash| self.get_block_ext(&hash))
    }

    fn max_target(&self) -> u64 {
        MAX_TARGET
    }
}
```

**File:** rpc/src/util/fee_rate.rs (L86-111)
```rust
        let mut fee_rates = self.provider.collect(target, |mut fee_rates, block_ext| {
            let BlockExt {
                txs_sizes,
                cycles,
                txs_fees,
                ..
            } = block_ext;
            let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
            if txs_sizes.len() > 1 && !txs_fees.is_empty() {
                // block_ext.txs_fees's length == block_ext.cycles's length
                // block_ext.txs_fees's length + 1 == txs_sizes's length
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
            fee_rates
        });
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
