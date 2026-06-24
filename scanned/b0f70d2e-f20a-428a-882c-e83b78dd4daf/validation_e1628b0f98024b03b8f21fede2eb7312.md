Audit Report

## Title
`get_fee_rate_statistics` Panics on `txs_sizes: None` from Legacy `BlockExt` Deserialization — (File: `rpc/src/util/fee_rate.rs`)

## Summary
The `Unpack<core::BlockExt>` implementation for the legacy 5-field `packed::BlockExtReader` hardcodes `txs_sizes: None`. The `BlockExt2019ToZero` migration re-serializes pre-hard-fork blocks into the 7-field `BlockExtV1` format while preserving `txs_sizes: None`. On any node whose recent 101-block sliding window contains such entries, `FeeRateCollector::statistics()` unconditionally calls `.expect("expect txs_size's length >= 1")` on the `None` value, causing a Rust panic in the RPC handler.

## Finding Description
**Root cause — silent `None` in legacy deserialization:**

`util/types/src/conversion/storage.rs` lines 139–151 hardcode `txs_sizes: None` for the 5-field legacy format: [1](#0-0) 

The 7-field `BlockExtV1Reader` correctly reads the field: [2](#0-1) 

**Dispatch in `get_block_ext`:**

`store/src/store.rs` dispatches on `count_extra_fields()`. A 5-field record (0 extra fields) takes the `reader.into()` path, returning `txs_sizes: None`: [3](#0-2) 

**Migration preserves `None`:**

`BlockExt2019ToZero` reads old block exts (which already have `txs_sizes: None` from the 5-field deserialization path), sets `cycles = None`, and re-inserts via `insert_block_ext`, which serializes using `Pack<packed::BlockExtV1>`. The result is a 7-field record on disk with `txs_sizes: None`: [4](#0-3) 

After migration those blocks take the `count_extra_fields() == 2` path in `get_block_ext` and still return `txs_sizes: None`.

**Panic site:**

`rpc/src/util/fee_rate.rs` line 93 calls `.expect()` unconditionally: [5](#0-4) 

The RPC entry point is publicly exposed without authentication: [6](#0-5) 

## Impact Explanation
A Rust `expect` panic in the RPC handler closure crashes or permanently breaks the `get_fee_rate_statistics` (and deprecated `get_fee_rate_statics`) endpoint until the node is restarted. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash.**

## Likelihood Explanation
The condition is met on any node that: (1) ran a pre-v0.106 binary storing blocks in 5-field `BlockExt` format, and (2) upgraded to v0.106+. After upgrade, the `BlockExt2019ToZero` migration converts pre-hard-fork blocks to 7-field format with `txs_sizes: None`, and any pre-v0.106 blocks after the hard fork epoch remain in 5-field format. While the chain tip on a long-running mainnet node is far beyond the 101-block window, nodes that recently upgraded or are syncing near the migration boundary are directly affected. No authentication is required — any caller with access to the RPC port can trigger the panic by calling `get_fee_rate_statistics`.

## Recommendation
Replace the unconditional `.expect()` in `rpc/src/util/fee_rate.rs` with a graceful skip:

```rust
let Some(txs_sizes) = txs_sizes else { return fee_rates; };
```

This matches the existing pattern for the `cycles` field (line 97), which uses `if let Some(cycles) = cycles` rather than panicking.

## Proof of Concept
1. Run a CKB node on a pre-v0.106 binary until the chain tip is beyond the CKB2021 hard fork epoch, storing blocks in 5-field `BlockExt` format.
2. Upgrade the binary. The `BlockExt2019ToZero` migration runs, converting pre-hard-fork blocks to 7-field format with `txs_sizes: None`; post-hard-fork blocks remain in 5-field format.
3. While the chain tip is within 101 blocks of the last pre-v0.106 block, call:
   ```json
   {"jsonrpc":"2.0","method":"get_fee_rate_statistics","params":[],"id":1}
   ```
4. `FeeRateCollector::collect` iterates the last 21 blocks (default target), hits a block with `txs_sizes: None`, and the `.expect()` at `rpc/src/util/fee_rate.rs:93` panics, crashing or permanently breaking the RPC handler.

### Citations

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

**File:** util/migrate/src/migrations/set_2019_block_cycle_zero.rs (L88-90)
```rust
                    let mut old_block_ext = db_txn.get_block_ext(&hash).unwrap();
                    old_block_ext.cycles = None;
                    db_txn.insert_block_ext(&hash, &old_block_ext)?;
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

**File:** rpc/src/module/chain.rs (L2129-2132)
```rust
    fn get_fee_rate_statistics(&self, target: Option<Uint64>) -> Result<Option<FeeRateStatistics>> {
        Ok(FeeRateCollector::new(self.shared.snapshot().as_ref())
            .statistics(target.map(Into::into)))
    }
```
