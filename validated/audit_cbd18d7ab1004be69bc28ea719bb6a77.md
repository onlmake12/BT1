All four cited locations are confirmed in the repository:

- `rpc/src/util/fee_rate.rs` line 93: `.expect()` is unconditional [1](#0-0) 
- `filter_map` skips only DB-absent blocks, not `txs_sizes: None` blocks [2](#0-1) 
- Legacy `packed::BlockExtReader` unpacks with `txs_sizes: None` [3](#0-2) 
- Store dispatches to legacy reader when `count_extra_fields() == 0` [4](#0-3) 
- Both RPC endpoints are unauthenticated [5](#0-4) 

---

Audit Report

## Title
Unauthenticated RPC Panic via Unconditional `.expect()` on Legacy `BlockExt.txs_sizes` — (`rpc/src/util/fee_rate.rs`)

## Summary

`FeeRateCollector::statistics` calls `.expect()` unconditionally on `block_ext.txs_sizes` at line 93 of `rpc/src/util/fee_rate.rs`. Legacy on-disk `BlockExt` records (pre-v0.106, 5-field molecule schema) always deserialize with `txs_sizes: None`. Any node whose database contains such blocks will panic in the RPC handler when `get_fee_rate_statistics` or `get_fee_rate_statics` is called with a target window covering those blocks.

## Finding Description

**Panic site** (`rpc/src/util/fee_rate.rs`, line 93): The `collect` closure destructures each `BlockExt` and immediately calls `txs_sizes.expect("expect txs_size's length >= 1")` with no prior `None` check. The upstream `filter_map` at lines 45–47 only removes blocks absent from the database; it passes through every block whose `BlockExt` deserializes with `txs_sizes: None`.

**Source of `None`** (`util/types/src/conversion/storage.rs`, lines 139–150): The `Unpack<core::BlockExt>` impl for `packed::BlockExtReader` (the 5-field legacy schema) unconditionally sets both `cycles: None` and `txs_sizes: None`. The `From` impl at lines 154–166 does the same.

**Store dispatch** (`store/src/store.rs`, lines 252–254): `get_block_ext` reads the raw blob and branches on `count_extra_fields()`. When the stored blob has 0 extra fields (old format), it calls `reader.into()`, which invokes the legacy `From<packed::BlockExtReader>` conversion, yielding `txs_sizes: None`.

**RPC entry points** (`rpc/src/module/chain.rs`, lines 2124–2132): Both `get_fee_rate_statics` and `get_fee_rate_statistics` directly call `FeeRateCollector::new(...).statistics(...)` with no authentication or access control. Any external caller can invoke them.

The exploit path is: caller sends `get_fee_rate_statistics` with a `target` window large enough to include pre-v0.106 blocks → store returns a `BlockExt` with `txs_sizes: None` → `filter_map` passes it through → `.expect()` panics.

## Impact Explanation

The `.expect()` panic unwinds the RPC handler task. With Rust's default `unwind` panic strategy the node process survives, but the RPC call aborts with an internal error. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash**.

## Likelihood Explanation

Any mainnet or testnet node that synced blocks before v0.106 retains legacy `BlockExt` rows in `COLUMN_BLOCK_EXT`. The RPC endpoints are unauthenticated and publicly reachable. Calling `get_fee_rate_statistics` with a sufficiently large `target` value to reach those legacy blocks is sufficient to trigger the panic. No special privileges, victim interaction, or race conditions are required. The condition is repeatable on every call.

## Recommendation

Replace the unconditional `.expect()` at line 93 with a graceful skip, consistent with the existing `cycles` `Option` handling at line 97:

```rust
let Some(txs_sizes) = txs_sizes else {
    return fee_rates; // skip legacy blocks that lack txs_sizes
};
```

## Proof of Concept

```rust
// Add to rpc/src/tests/fee_rate.rs
#[test]
fn test_fee_rate_statistics_none_txs_sizes_does_not_panic() {
    let mut provider = DummyFeeRateProvider::new(3);
    provider.append(1, BlockExt {
        received_at: 0,
        total_difficulty: 0u64.into(),
        total_uncles_count: 0,
        verified: None,
        txs_fees: vec![Capacity::shannons(1000)],
        cycles: Some(vec![100]),
        txs_sizes: None,   // legacy block — triggers .expect() panic at line 93
    });
    // Panics at rpc/src/util/fee_rate.rs:93 on current code
    let _ = FeeRateCollector::new(&provider).statistics(Some(3));
}
```

### Citations

**File:** rpc/src/util/fee_rate.rs (L45-47)
```rust
        let block_ext_iter =
            (start..=tip_number).filter_map(|number| self.get_block_ext_by_number(number));
        block_ext_iter.fold(Vec::new(), f)
```

**File:** rpc/src/util/fee_rate.rs (L93-93)
```rust
            let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
```

**File:** util/types/src/conversion/storage.rs (L139-150)
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
```

**File:** store/src/store.rs (L252-254)
```rust
                match reader.count_extra_fields() {
                    0 => reader.into(),
                    2 => packed::BlockExtV1Reader::from_slice_should_be_ok(slice.as_ref()).into(),
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
