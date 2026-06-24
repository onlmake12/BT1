Audit Report

## Title
Unconditional `.expect()` on `txs_sizes` in `FeeRateCollector::statistics` Panics on Legacy `BlockExt` Records — (`rpc/src/util/fee_rate.rs`)

## Summary
`FeeRateCollector::statistics` unconditionally calls `.expect()` on `block_ext.txs_sizes` at line 93 of `rpc/src/util/fee_rate.rs`. Blocks stored in the legacy 5-field `BlockExt` format (pre-v0.106) deserialize with `txs_sizes: None`. When the RPC's iteration window covers such a block, the `.expect()` panics, crashing the RPC worker thread and rendering the node's RPC interface non-functional.

## Finding Description
**Panic site** — `rpc/src/util/fee_rate.rs:93`: [1](#0-0) 

There is no `None` guard before this line. If `txs_sizes` is `None`, the thread panics unconditionally.

**Root cause** — The legacy `BlockExtReader::unpack` hardcodes `txs_sizes: None`: [2](#0-1) 

The same applies to the `From` impl at lines 154–166. [3](#0-2) 

**Storage dispatch** — `get_block_ext` returns the legacy struct (with `txs_sizes: None`) for any block stored in the 5-field format: [4](#0-3) 

**No complete migration** — The only migration touching `BlockExt` records (`BlockExt2019ToZero`) returns early when `limit_epoch == 0` (e.g., devnet configurations), leaving all legacy records unconverted: [5](#0-4) 

No migration in `util/migrate/src/migrations/mod.rs` performs a full `BlockExt` → `BlockExtV1` conversion for all blocks. [6](#0-5) 

**Iteration window** — `collect` iterates from `max(1, tip_number + 1 − target)` to `tip_number`: [7](#0-6) 

With `target` capped at 101, any node whose tip is ≤ 101 and whose early blocks are in legacy format will have the window cover those legacy records.

**RPC entry points** — both `get_fee_rate_statics` and `get_fee_rate_statistics` call `statistics` with no authentication: [8](#0-7) 

## Impact Explanation
A Rust `.expect()` failure panics the calling thread. In the jsonrpc-core thread pool, this kills the worker thread handling the request. Without a `catch_unwind` boundary (none found in the RPC layer), repeated calls can exhaust the thread pool or crash the process, making the node's RPC interface non-functional.

**Concrete allowed impact: Note — Any local RPC API crash (0–500 pts).**

## Likelihood Explanation
- The Chain RPC module is enabled by default; no credentials are required.
- The RPC binds to `127.0.0.1:8114` by default, requiring only local (unprivileged) access.
- Devnet nodes with `limit_epoch == 0` skip the `BlockExt2019ToZero` migration entirely, leaving all early blocks in legacy format.
- Any node whose tip was below 101 while running pre-v0.106 software retains legacy `BlockExt` records that are never rewritten.
- A single unauthenticated HTTP POST is sufficient to trigger the panic.

## Recommendation
Replace the unconditional `.expect()` with a graceful `None` skip:

```rust
// Instead of:
let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");

// Use:
let Some(txs_sizes) = txs_sizes else { return fee_rates; };
```

This silently skips legacy blocks (which carry no fee-rate data anyway) rather than panicking.

## Proof of Concept
1. Start a CKB devnet node with pre-v0.106 database (or a node whose genesis/early blocks were stored in 5-field `BlockExt` format and whose tip is ≤ 101).
2. Send a single unauthenticated RPC call:

```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"get_fee_rate_statistics","params":[]}'
```

3. The RPC worker thread panics at `rpc/src/util/fee_rate.rs:93` with `'expect txs_size's length >= 1'`. The node's RPC becomes unresponsive or the process exits.

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

**File:** util/migrate/src/migrations/set_2019_block_cycle_zero.rs (L41-43)
```rust
        if limit_epoch == 0 {
            return Ok(chain_db.into_inner());
        }
```

**File:** util/migrate/src/migrations/mod.rs (L1-19)
```rust
mod add_block_extension_cf;
mod add_block_filter;
mod add_block_filter_hash;
mod add_chain_root_mmr;
mod add_extra_data_hash;
mod add_number_hash_mapping;
mod cell;
mod set_2019_block_cycle_zero;
mod table_to_struct;

pub use add_block_extension_cf::AddBlockExtensionColumnFamily;
pub use add_block_filter::AddBlockFilterColumnFamily;
pub use add_block_filter_hash::AddBlockFilterHash;
pub use add_chain_root_mmr::AddChainRootMMR;
pub use add_extra_data_hash::AddExtraDataHash;
pub use add_number_hash_mapping::AddNumberHashMapping;
pub use cell::CellMigration;
pub use set_2019_block_cycle_zero::BlockExt2019ToZero;
pub use table_to_struct::ChangeMoleculeTableToStruct;
```

**File:** rpc/src/module/chain.rs (L2124-2132)
```rust
    fn get_fee_rate_statics(&self, target: Option<Uint64>) -> Result<Option<FeeRateStatistics>> {
        Ok(FeeRateCollector::new(self.shared.snapshot().as_ref())
            .statistics(target.map(Into::into)))
    }

    fn get_fee_rate_statistics(&self, target: Option<Uint64>) -> Result<Option<FeeRateStatistics>> {
        Ok(FeeRateCollector::new(self.shared.snapshot().as_ref())
            .statistics(target.map(Into::into)))
    }
```
