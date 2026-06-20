### Title
Unconditional `expect()` on `None` `txs_sizes` in `FeeRateCollector::statistics` Panics RPC Handler for Nodes with Pre-Migration Blocks — (`rpc/src/util/fee_rate.rs`)

---

### Summary

`FeeRateCollector::statistics` calls `.expect()` unconditionally on `BlockExt::txs_sizes`, which is hardcoded to `None` when deserializing old-format (`packed::BlockExt`) blocks. Any node that has been running since before the `BlockExtV1` storage format was introduced will have historical blocks with `txs_sizes = None` in the database. Calling `get_fee_rate_statistics` with a target window that reaches those blocks causes an immediate panic in the RPC handler.

---

### Finding Description

**The unconditional `expect()` call:**

In `rpc/src/util/fee_rate.rs`, the closure passed to `collect` does:

```rust
let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
``` [1](#0-0) 

There is no prior guard that checks whether `txs_sizes` is `Some` before calling `expect`. The `collect` helper only skips blocks where `get_block_ext_by_number` returns `None` (block not found), not blocks where the returned `BlockExt` has `txs_sizes = None`. [2](#0-1) 

**Why `txs_sizes` is `None` for old blocks:**

The codebase has two on-disk formats for `BlockExt`: the original `packed::BlockExt` and the newer `packed::BlockExtV1`. When deserializing from the old format, `txs_sizes` (and `cycles`) are hardcoded to `None`:

```rust
impl<'r> Unpack<core::BlockExt> for packed::BlockExtReader<'r> {
    fn unpack(&self) -> core::BlockExt {
        core::BlockExt {
            ...
            cycles: None,
            txs_sizes: None,   // <-- always None for old-format blocks
        }
    }
}
``` [3](#0-2) 

The `BlockExtV1` deserialization path correctly populates both fields: [4](#0-3) 

**The migration does not backfill old blocks:**

The `AddBlockExtensionColumnFamily` migration simply adds a new column family and returns — it does not rewrite existing `BlockExt` records into the `BlockExtV1` format. Old blocks remain in the old format with `txs_sizes = None` permanently. [5](#0-4) 

**`BlockExt::txs_sizes` is declared `Option<Vec<u64>>`**, explicitly allowing `None`: [6](#0-5) 

---

### Impact Explanation

When `get_fee_rate_statistics` (or the deprecated `get_fee_rate_statics`) is called on a node whose chain history predates the `BlockExtV1` format, the `collect` iterator will yield `BlockExt` values with `txs_sizes = None`. The unconditional `.expect()` panics. Depending on whether the jsonrpc framework uses `catch_unwind`, this either crashes the RPC handler thread (dropping the connection) or terminates the node process. At minimum, every RPC call with a window touching pre-migration blocks fails with a panic. [7](#0-6) 

---

### Likelihood Explanation

Any mainnet or long-running testnet node that was first synced before the `BlockExtV1` format was introduced has pre-migration blocks permanently stored in the old format. The `get_fee_rate_statistics` RPC is publicly accessible to any unprivileged caller. The default target window is 21 blocks, but the maximum is 101 — on a node where the tip is within 101 blocks of the migration boundary, the panic is trivially reachable. On a fully-synced mainnet node, the window would need to be large enough to reach pre-migration block numbers, which is only possible if the tip is near the migration point or if the node was recently synced from a snapshot that includes old-format blocks.

---

### Recommendation

Replace the unconditional `.expect()` with a guard that skips blocks where `txs_sizes` is `None`:

```rust
let txs_sizes = match txs_sizes {
    Some(s) => s,
    None => return fee_rates, // skip pre-migration blocks gracefully
};
``` [8](#0-7) 

---

### Proof of Concept

Using the existing `DummyFeeRateProvider` test harness, add a block with `txs_sizes: None`:

```rust
provider.append(5, BlockExt {
    received_at: 0,
    total_difficulty: 0u64.into(),
    total_uncles_count: 0,
    verified: None,
    txs_fees: vec![],
    cycles: None,
    txs_sizes: None,  // simulates pre-migration block
});
// This call panics:
let _ = FeeRateCollector::new(&provider).statistics(Some(21));
``` [9](#0-8) 

The `expect()` on line 93 of `fee_rate.rs` panics with `"expect txs_size's length >= 1"` whenever a pre-migration block falls within the query window.

### Citations

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

**File:** util/types/src/conversion/storage.rs (L203-215)
```rust
impl<'r> Unpack<core::BlockExt> for packed::BlockExtV1Reader<'r> {
    fn unpack(&self) -> core::BlockExt {
        core::BlockExt {
            received_at: self.received_at().into(),
            total_difficulty: self.total_difficulty().into(),
            total_uncles_count: self.total_uncles_count().into(),
            verified: self.verified().into(),
            txs_fees: self.txs_fees().into(),
            cycles: self.cycles().into(),
            txs_sizes: self.txs_sizes().into(),
        }
    }
}
```

**File:** util/migrate/src/migrations/add_block_extension_cf.rs (L9-25)
```rust
impl Migration for AddBlockExtensionColumnFamily {
    fn migrate(
        &self,
        db: RocksDB,
        _pb: Arc<dyn Fn(u64) -> ProgressBar + Send + Sync>,
    ) -> Result<RocksDB> {
        Ok(db)
    }

    fn version(&self) -> &str {
        VERSION
    }

    fn expensive(&self) -> bool {
        false
    }
}
```

**File:** util/types/src/core/extras.rs (L38-41)
```rust
    /// Sizes of each transaction including the cellbase.
    /// The length of `txs_sizes` is `txs_fees` length + 1.
    pub txs_sizes: Option<Vec<u64>>,
}
```

**File:** rpc/src/module/chain.rs (L2129-2132)
```rust
    fn get_fee_rate_statistics(&self, target: Option<Uint64>) -> Result<Option<FeeRateStatistics>> {
        Ok(FeeRateCollector::new(self.shared.snapshot().as_ref())
            .statistics(target.map(Into::into)))
    }
```

**File:** rpc/src/tests/fee_rate.rs (L1-45)
```rust
use crate::util::{FeeRateCollector, FeeRateProvider};
use ckb_jsonrpc_types::FeeRateStatistics;
use ckb_types::core::{BlockExt, BlockNumber, Capacity};
use std::collections::HashMap;

struct DummyFeeRateProvider {
    tip_number: BlockNumber,
    block_exts: HashMap<BlockNumber, BlockExt>,
    max_target: u64,
}

impl DummyFeeRateProvider {
    pub fn new(max_target: u64) -> DummyFeeRateProvider {
        DummyFeeRateProvider {
            tip_number: 0,
            block_exts: HashMap::new(),
            max_target,
        }
    }

    pub fn append(&mut self, number: BlockNumber, ext: BlockExt) {
        if number > self.tip_number {
            self.tip_number = number;
        }
        self.block_exts.insert(number, ext);
    }

    pub fn set_max_target(&mut self, max_target: u64) {
        self.max_target = max_target
    }
}

impl FeeRateProvider for DummyFeeRateProvider {
    fn get_tip_number(&self) -> BlockNumber {
        self.tip_number
    }

    fn get_block_ext_by_number(&self, number: BlockNumber) -> Option<BlockExt> {
        self.block_exts.get(&number).cloned()
    }

    fn max_target(&self) -> u64 {
        self.max_target
    }
}
```
