Audit Report

## Title
RPC Handler Panic via Unconditional `.expect()` on `BlockExt.txs_sizes` — (`rpc/src/util/fee_rate.rs`)

## Summary

`FeeRateCollector::statistics` calls `.expect()` unconditionally on `BlockExt.txs_sizes`, which is `Option<Vec<u64>>`. Blocks stored in the legacy 5-field `BlockExt` schema (pre-v0.106) deserialize with `txs_sizes: None`. Any unprivileged caller invoking `get_fee_rate_statistics` or `get_fee_rate_statics` on a node whose recent-block window contains such records triggers a panic in the RPC handler thread, crashing that endpoint.

## Finding Description

**Unconditional `.expect()` at the call site:**
`rpc/src/util/fee_rate.rs` line 93 calls `txs_sizes.expect("expect txs_size's length >= 1")` with no prior `None` guard inside the closure passed to `collect`. [1](#0-0) 

**`collect` does not filter `None`-`txs_sizes` blocks:**
The `filter_map` at line 46 only drops entries where `get_block_ext_by_number` itself returns `None` (block not found). A `BlockExt` that exists but carries `txs_sizes: None` passes through unfiltered and reaches the `expect`. [2](#0-1) 

**Two on-disk formats; the old one always yields `txs_sizes: None`:**
The molecule schema defines `BlockExt` (5 fields, pre-v0.106) without `txs_sizes`, and `BlockExtV1` (7 fields) which added `cycles` and `txs_sizes` as `Uint64VecOpt`. [3](#0-2) 

`get_block_ext` dispatches on `count_extra_fields()`: when it is `0` (old format), `reader.into()` is used, which hard-codes `txs_sizes: None`. [4](#0-3) [5](#0-4) 

**Background migration does not protect the window:**
`BlockExt2019ToZero` declares `run_in_background() -> true`, so the node begins serving RPC requests while migration is still running. [6](#0-5) 

Furthermore, the migration reads old records (which have `txs_sizes: None`), sets `cycles = None`, and re-writes via `insert_block_ext`. Since `insert_block_ext` always serializes as `BlockExtV1` but preserves the `txs_sizes: None` value from the in-memory struct, the re-written records are in `BlockExtV1` format yet still deserialize to `txs_sizes: None`. The panic path remains open even after migration completes. [7](#0-6) [8](#0-7) 

## Impact Explanation

A panic in the RPC handler thread crashes that handler. The node process itself is not terminated. The impact is scoped to availability of the `get_fee_rate_statistics` / `get_fee_rate_statics` RPC endpoints. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash**.

## Likelihood Explanation

The precondition — at least one block with `txs_sizes: None` inside the last `target` (≤ 101) blocks — is realistic on:
- Any node upgrading from pre-v0.106 while the background migration is still processing recent blocks.
- Any node whose chain tip is still within the pre-v0.106 block range (syncing from genesis, testnet with few blocks, private chain).
- Any node where the migration re-wrote old blocks as `BlockExtV1` with `txs_sizes: None` and those blocks fall within the 101-block window.

On a fully-synced mainnet node where all recent blocks were verified by v0.106+ code, the last 101 blocks will have `txs_sizes` populated and the panic will not trigger. Risk is highest during upgrade windows and on non-mainnet deployments.

## Recommendation

Replace the unconditional `expect` with a graceful skip, consistent with the existing `cycles` handling at line 97:

```rust
// rpc/src/util/fee_rate.rs, inside the collect closure
let txs_sizes = match txs_sizes {
    Some(v) => v,
    None => return fee_rates, // skip pre-migration blocks silently
};
```

This mirrors the `if let Some(cycles) = cycles` pattern already used for the other optional field. [9](#0-8) 

## Proof of Concept

Extend the existing unit-test harness in `rpc/src/tests/fee_rate.rs` with a provider that returns a `BlockExt { txs_sizes: None, ... }`:

```rust
#[test]
fn test_fee_rate_none_txs_sizes_does_not_panic() {
    let mut provider = DummyFeeRateProvider::new(5);
    provider.append(1, BlockExt {
        received_at: 0,
        total_difficulty: 0u64.into(),
        total_uncles_count: 0,
        verified: Some(true),
        txs_fees: vec![Capacity::shannons(1000)],
        cycles: Some(vec![100]),
        txs_sizes: None,   // simulates pre-migration / old-format block
    });
    // Panics at rpc/src/util/fee_rate.rs:93 on unpatched code
    let _ = FeeRateCollector::new(&provider).statistics(None);
}
```

Running this test against the current code reproduces the panic at `rpc/src/util/fee_rate.rs:93`. [10](#0-9)

### Citations

**File:** rpc/src/util/fee_rate.rs (L45-47)
```rust
        let block_ext_iter =
            (start..=tip_number).filter_map(|number| self.get_block_ext_by_number(number));
        block_ext_iter.fold(Vec::new(), f)
```

**File:** rpc/src/util/fee_rate.rs (L86-108)
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
```

**File:** util/gen-types/schemas/extensions.mol (L66-82)
```text
table BlockExt {
    total_difficulty:   Uint256,
    total_uncles_count: Uint64,
    received_at:        Uint64,
    txs_fees:           Uint64Vec,
    verified:           BoolOpt,
}

table BlockExtV1 {
    total_difficulty:   Uint256,
    total_uncles_count: Uint64,
    received_at:        Uint64,
    txs_fees:           Uint64Vec,
    verified:           BoolOpt,
    cycles:             Uint64VecOpt,
    txs_sizes:          Uint64VecOpt,
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

**File:** util/migrate/src/migrations/set_2019_block_cycle_zero.rs (L23-26)
```rust
impl Migration for BlockExt2019ToZero {
    fn run_in_background(&self) -> bool {
        true
    }
```

**File:** util/migrate/src/migrations/set_2019_block_cycle_zero.rs (L86-90)
```rust
                for _ in 0..10000 {
                    let hash = header.hash();
                    let mut old_block_ext = db_txn.get_block_ext(&hash).unwrap();
                    old_block_ext.cycles = None;
                    db_txn.insert_block_ext(&hash, &old_block_ext)?;
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
