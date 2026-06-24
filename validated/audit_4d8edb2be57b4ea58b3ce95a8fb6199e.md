All cited code references are confirmed accurate. Here is the validation:

---

Audit Report

## Title
Unconditional `.expect()` on `txs_sizes` in `FeeRateCollector::statistics` panics on legacy `BlockExt` records — (`rpc/src/util/fee_rate.rs`)

## Summary

`FeeRateCollector::statistics` unconditionally calls `.expect("expect txs_size's length >= 1")` on `block_ext.txs_sizes`, which is an `Option<Vec<u32>>`. The store's compatibility reader for pre-v0.106 `BlockExt` records always deserialises `txs_sizes` as `None`. Any node whose canonical chain's last ≤101 blocks include at least one legacy-format record will panic when `get_fee_rate_statistics` or `get_fee_rate_statics` is called by any unauthenticated caller.

## Finding Description

**Panic site** — `rpc/src/util/fee_rate.rs:93`: [1](#0-0) 

The closure passed to `collect` unconditionally calls `.expect()` on `txs_sizes`, which is `Option<Vec<u32>>`.

**Why `txs_sizes` can be `None`** — `store/src/store.rs:252-253` dispatches on `count_extra_fields()`. When a record was written in the legacy 5-field `packed::BlockExt` format (before v0.106), `count_extra_fields()` returns `0` and the reader is converted via `reader.into()`: [2](#0-1) 

That conversion is implemented in `util/types/src/conversion/storage.rs`, which hard-codes both `cycles` and `txs_sizes` to `None` in both the `Unpack` impl (lines 139–151) and the `From` impl (lines 154–165): [3](#0-2) [4](#0-3) 

**Why `filter_map` does not protect the caller** — `collect` uses `filter_map` only to skip blocks whose hash or ext is absent from the DB. A legacy block returns `Some(BlockExt { txs_sizes: None, … })`, which passes through the iterator and reaches the `.expect()` call: [5](#0-4) 

**New blocks are always written as `BlockExtV1`** — `store/src/transaction.rs:241-252` always packs as `packed::BlockExtV1`, so only blocks committed before v0.106 carry the legacy format: [6](#0-5) 

**RPC entry points** — both `get_fee_rate_statics` and `get_fee_rate_statistics` call `FeeRateCollector::statistics` with no authentication: [7](#0-6) 

## Impact Explanation

A Rust `panic!` in the synchronous RPC handler crashes the handler for that request. Repeated unauthenticated calls keep triggering the panic, making the RPC endpoint persistently unavailable. This matches the allowed CKB bounty impact: **Note — Any local RPC API crash (0–500 pts)**.

## Likelihood Explanation

On a long-running mainnet node all recent blocks are `BlockExtV1`, so the window of last ≤101 blocks never reaches legacy records. The condition is realistic for: testnets or devnets with short chains upgraded from a pre-v0.106 binary; any node whose chain height at upgrade time was ≤101 blocks; integration-test environments replaying a short chain from genesis with an old DB snapshot. The RPC requires no authentication and accepts any `target` value from 1 to 101.

## Recommendation

Replace the unconditional `.expect()` with a graceful skip, matching the existing guard on `cycles` at line 97:

```rust
// Before
let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");

// After
let Some(txs_sizes) = txs_sizes else { return fee_rates; };
```

## Proof of Concept

1. Open a `ChainDB` and insert a genesis block whose `BlockExt` is serialised in the legacy 5-field `packed::BlockExt` format (i.e., **not** `BlockExtV1`) — `txs_sizes` field absent.
2. Attach the block to the main chain index via `attach_block`.
3. Wrap the store in a `Snapshot` and call `FeeRateCollector::new(snapshot.as_ref()).statistics(Some(1))`.
4. Observe the process panics at `rpc/src/util/fee_rate.rs:93` with message `"expect txs_size's length >= 1"`.

The existing test at `store/src/tests/db.rs:54-68` already constructs a `BlockExt { txs_sizes: None }` and round-trips it through the store: [8](#0-7) 

Extending that test to attach the block to the main chain index and then call `FeeRateCollector::statistics` is sufficient to reproduce the panic without any additional infrastructure.

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

**File:** store/src/store.rs (L252-254)
```rust
                match reader.count_extra_fields() {
                    0 => reader.into(),
                    2 => packed::BlockExtV1Reader::from_slice_should_be_ok(slice.as_ref()).into(),
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

**File:** store/src/transaction.rs (L241-252)
```rust
    pub fn insert_block_ext(
        &self,
        block_hash: &packed::Byte32,
        ext: &BlockExt,
    ) -> Result<(), Error> {
        let packed_ext: packed::BlockExtV1 = ext.into();
        self.insert_raw(
            COLUMN_BLOCK_EXT,
            block_hash.as_slice(),
            packed_ext.as_slice(),
        )
    }
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

**File:** store/src/tests/db.rs (L54-68)
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

    let hash = block.hash();
    let txn = store.begin_transaction();
    txn.insert_block_ext(&hash, &ext).unwrap();
    txn.commit().unwrap();
    assert_eq!(ext, store.get_block_ext(&hash).unwrap());
```
