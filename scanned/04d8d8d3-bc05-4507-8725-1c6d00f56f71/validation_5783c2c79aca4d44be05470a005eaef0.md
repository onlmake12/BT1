### Title
Hardcoded `None` for `txs_sizes` in Legacy `BlockExt` Deserialization Causes Node Panic via `get_fee_rate_statistics` RPC - (File: `util/types/src/conversion/storage.rs`)

---

### Summary

When deserializing old-format (5-field, pre-v0.106) `BlockExt` records from the database, the `cycles` and `txs_sizes` fields are unconditionally hardcoded to `None`. The `get_fee_rate_statistics` (and `get_fee_rate_statics`) RPC handler then calls `.expect(...)` on `txs_sizes` without guarding against `None`, causing a node panic (process crash) whenever the fee-rate statistics window includes any such legacy block.

---

### Finding Description

`BlockExt` was extended in v0.106 from a 5-field molecule table (`BlockExt`) to a 7-field table (`BlockExtV1`) that added `cycles` and `txs_sizes`. The deserialization path for the old 5-field format in `util/types/src/conversion/storage.rs` hardcodes both new fields to `None`:

```rust
impl<'r> Unpack<core::BlockExt> for packed::BlockExtReader<'r> {
    fn unpack(&self) -> core::BlockExt {
        core::BlockExt {
            ...
            cycles: None,    // hardcoded
            txs_sizes: None, // hardcoded
        }
    }
}
```

The `get_block_ext` function in `store/src/store.rs` dispatches on the field count: 5-field records go through the old reader (returning `txs_sizes: None`), while 7-field records go through `BlockExtV1Reader` (which properly unpacks both fields).

The `get_fee_rate_statistics` RPC calls `FeeRateCollector::statistics`, which iterates over recent blocks and calls `.expect(...)` unconditionally on `txs_sizes`:

```rust
let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
```

If any block in the collection window has `txs_sizes: None` (i.e., was stored in the old 5-field format), this `expect` panics and crashes the node process.

---

### Impact Explanation

An unprivileged RPC caller can crash the CKB node process by calling `get_fee_rate_statistics` (or the deprecated `get_fee_rate_statics`) on a node whose database contains legacy 5-field `BlockExt` records within the last 1–101 blocks of the chain. The panic terminates the node, causing a complete denial of service until the operator manually restarts it.

---

### Likelihood Explanation

Nodes that were upgraded from a version prior to v0.106 retain old-format `BlockExt` records for all blocks processed before the upgrade. If the upgrade occurred fewer than 101 blocks before the current tip (e.g., on a testnet, private network, or a node that was offline for a long time and then upgraded), the statistics window will include legacy blocks. Any RPC caller — including an unauthenticated one if the RPC port is exposed — can trigger the panic with a single call. On mainnet with a long chain, the window of 101 blocks is unlikely to reach pre-v0.106 blocks, but the code path remains permanently broken for any node in the described state.

---

### Recommendation

1. **Guard the `expect` call**: Replace the unconditional `.expect(...)` with a `match` or `if let Some(...)` check, and skip blocks where `txs_sizes` is `None` rather than panicking.
2. **Normalize on read**: In the 5-field deserialization path (`Unpack<core::BlockExt> for packed::BlockExtReader`), consider returning `None` from `get_block_ext_by_number` for blocks that lack `txs_sizes`, or skip them in the `collect` iterator.

---

### Proof of Concept

1. Run a CKB node that was upgraded from pre-v0.106 to a current version, with fewer than 101 blocks mined after the upgrade (or use a private/test network).
2. Send the following RPC call to the node:
   ```json
   {"id": 1, "jsonrpc": "2.0", "method": "get_fee_rate_statistics", "params": []}
   ```
3. The node process panics with `expect("expect txs_size's length >= 1")` because `FeeRateCollector::statistics` encounters a `BlockExt` with `txs_sizes: None` deserialized from a legacy 5-field database record.

**Root cause chain:**

- `get_fee_rate_statistics` RPC → `FeeRateCollector::statistics` (`rpc/src/util/fee_rate.rs:93`) [1](#0-0) 
- → `get_block_ext` dispatches to old reader for 5-field records (`store/src/store.rs:252-253`) [2](#0-1) 
- → `Unpack<core::BlockExt> for packed::BlockExtReader` hardcodes `txs_sizes: None` (`util/types/src/conversion/storage.rs:147-148`) [3](#0-2) 
- → `txs_sizes.expect(...)` panics (`rpc/src/util/fee_rate.rs:93`) [4](#0-3) 

The `BlockExt` struct definition confirms `txs_sizes` is `Option<Vec<u64>>` and can legitimately be `None` for legacy records: [5](#0-4) 

The RPC entry point: [6](#0-5)

### Citations

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

**File:** rpc/src/module/chain.rs (L2129-2132)
```rust
    fn get_fee_rate_statistics(&self, target: Option<Uint64>) -> Result<Option<FeeRateStatistics>> {
        Ok(FeeRateCollector::new(self.shared.snapshot().as_ref())
            .statistics(target.map(Into::into)))
    }
```
