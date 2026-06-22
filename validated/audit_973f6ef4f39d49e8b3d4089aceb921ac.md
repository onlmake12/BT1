### Title
`get_fee_rate_statistics` RPC Panics Due to Legacy `BlockExt` Deserialization Silently Dropping `txs_sizes` — (File: `util/types/src/conversion/storage.rs`)

---

### Summary

The `Unpack<core::BlockExt>` implementation for the legacy packed `BlockExtReader` (5-field format) unconditionally hardcodes `cycles: None` and `txs_sizes: None`, silently discarding those fields. This is the direct CKB analog of the Sundial `From<SundialInitConfigParams>` implementation that silently zeroed `liquidity_decimals` via `..SundialConfig::default()`. The `get_fee_rate_statistics` RPC then calls `.expect("expect txs_size's length >= 1")` on the resulting `txs_sizes`, which panics when the field is `None`, crashing or erroring the RPC handler for any unprivileged caller who reaches a node whose recent block window contains such entries.

---

### Finding Description

**Root cause — silent field drop in deserialization:**

In `util/types/src/conversion/storage.rs`, both the `Unpack` and `From` implementations for the legacy 5-field `packed::BlockExtReader` hardcode the two newer fields to `None`:

```rust
impl<'r> Unpack<core::BlockExt> for packed::BlockExtReader<'r> {
    fn unpack(&self) -> core::BlockExt {
        core::BlockExt {
            received_at: self.received_at().into(),
            total_difficulty: self.total_difficulty().into(),
            total_uncles_count: self.total_uncles_count().into(),
            verified: self.verified().into(),
            txs_fees: self.txs_fees().into(),
            cycles: None,      // silently dropped
            txs_sizes: None,   // silently dropped
        }
    }
}
``` [1](#0-0) 

The newer 7-field `packed::BlockExtV1Reader` correctly reads both fields:

```rust
impl<'r> Unpack<core::BlockExt> for packed::BlockExtV1Reader<'r> {
    fn unpack(&self) -> core::BlockExt {
        core::BlockExt {
            ...
            cycles: self.cycles().into(),
            txs_sizes: self.txs_sizes().into(),
        }
    }
}
``` [2](#0-1) 

**Dispatch in `get_block_ext`:**

`ChainStore::get_block_ext` dispatches on `count_extra_fields()`. When the stored record has 0 extra fields (legacy format), it uses the `BlockExtReader` path, producing `txs_sizes: None`:

```rust
match reader.count_extra_fields() {
    0 => reader.into(),   // → cycles: None, txs_sizes: None
    2 => packed::BlockExtV1Reader::from_slice_should_be_ok(slice.as_ref()).into(),
    _ => panic!(...),
}
``` [3](#0-2) 

**Panic site in `get_fee_rate_statistics`:**

`FeeRateCollector::statistics()` calls `.expect()` unconditionally on `txs_sizes`:

```rust
let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
``` [4](#0-3) 

The `core::BlockExt` struct documents that `txs_sizes` is required for fee-rate accounting: [5](#0-4) 

The RPC entry point is publicly exposed: [6](#0-5) 

**Migration preserves the `None`:**

The `BlockExt2019ToZero` migration reads old block exts (which have `txs_sizes: None` because they were stored before the field existed), sets `cycles = None`, and re-inserts — preserving `txs_sizes: None` in the new 7-field format on disk:

```rust
let mut old_block_ext = db_txn.get_block_ext(&hash).unwrap();
old_block_ext.cycles = None;
db_txn.insert_block_ext(&hash, &old_block_ext)?;
``` [7](#0-6) 

After migration those blocks are stored in `BlockExtV1` format (7 fields) with `txs_sizes: None`, so `get_block_ext` takes the `count_extra_fields() == 2` path and returns `txs_sizes: None` — still triggering the panic.

---

### Impact Explanation

Any unprivileged RPC caller who invokes `get_fee_rate_statistics` (or the deprecated `get_fee_rate_statics`) on a node whose sliding window of up to 101 recent blocks contains at least one entry with `txs_sizes: None` will trigger an unconditional Rust `expect` panic inside the RPC handler closure. Depending on whether the JSON-RPC runtime catches the unwind, this results in either a hard node crash (process abort) or an unhandled internal error that permanently breaks fee-rate estimation for that node until it is restarted. Either outcome constitutes a remotely triggerable denial-of-service against the RPC subsystem.

---

### Likelihood Explanation

The condition is met on any node that:
1. Was running a pre-v0.106 binary (which stored `BlockExt` without `txs_sizes`) and then upgraded, **and**
2. Has not yet advanced the chain tip more than 101 blocks past the last migrated block.

This window is narrow on a long-running mainnet node but is fully reachable on nodes that recently upgraded or on nodes syncing a chain that crossed the migration boundary recently. The attacker needs only to call the public `get_fee_rate_statistics` RPC — no authentication, no special privilege, no hashpower.

---

### Recommendation

Replace the unconditional `.expect()` with a graceful skip or early return when `txs_sizes` is `None`:

```rust
let Some(txs_sizes) = txs_sizes else { return fee_rates; };
```

Additionally, the `Unpack` implementation for `packed::BlockExtReader` should be audited: if the legacy format genuinely cannot carry `txs_sizes`, callers that require it must guard against `None` rather than panic.

---

### Proof of Concept

1. Run a CKB node on a version prior to v0.106 so that blocks are stored in the legacy 5-field `BlockExt` format (no `txs_sizes`).
2. Upgrade the node binary. The `BlockExt2019ToZero` migration re-writes those blocks in 7-field format but with `txs_sizes: None`.
3. While the chain tip is within 101 blocks of the migration boundary, send an RPC call:
   ```json
   {"jsonrpc":"2.0","method":"get_fee_rate_statistics","params":[],"id":1}
   ```
4. `FeeRateCollector::collect` iterates the last 21 blocks (default target), hits a block with `txs_sizes: None`, and the closure panics at `

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

**File:** util/types/src/core/extras.rs (L13-21)
```rust
/// Represents a block's additional information.
///
/// It is crucial to ensure that `txs_sizes` has one more element than `txs_fees`, and that `cycles` has the same length as `txs_fees`.
///
/// `BlockTxsVerifier::verify()` skips the first transaction (the cellbase) in the block. Therefore, `txs_sizes` must have a length equal to `txs_fees` length + 1.
///
/// Refer to: https://github.com/nervosnetwork/ckb/blob/44afc93cd88a1b52351831dce788d3023c52f37e/verification/contextual/src/contextual_block_verifier.rs#L455
///
/// Additionally, the `get_fee_rate_statistics` RPC function requires accurate `txs_sizes` and `txs_fees` data from `BlockExt`.
```

**File:** rpc/src/module/chain.rs (L2129-2132)
```rust
    fn get_fee_rate_statistics(&self, target: Option<Uint64>) -> Result<Option<FeeRateStatistics>> {
        Ok(FeeRateCollector::new(self.shared.snapshot().as_ref())
            .statistics(target.map(Into::into)))
    }
```

**File:** util/migrate/src/migrations/set_2019_block_cycle_zero.rs (L88-90)
```rust
                    let mut old_block_ext = db_txn.get_block_ext(&hash).unwrap();
                    old_block_ext.cycles = None;
                    db_txn.insert_block_ext(&hash, &old_block_ext)?;
```
