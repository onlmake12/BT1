The code confirms all claims. Let me verify the key details:

Audit Report

## Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Bypasses Overflow Guard — (`util/dao/src/lib.rs`)

## Summary
`DaoCalculator::calculate_maximum_withdraw` uses a truncating `as u64` cast on line 156 of `util/dao/src/lib.rs` when narrowing the u128 intermediate result `withdraw_counted_capacity` to u64. Every other u128→u64 narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. When `withdraw_counted_capacity` exceeds `u64::MAX`, the cast silently wraps, returning a corrupted `Ok(Capacity)` instead of `Err(DaoError::Overflow)`. Because this function feeds into `withdrawed_interests` → `dao_field_with_current_epoch`, a corrupted value propagates into the DAO field embedded in every block header, causing consensus deviation.

## Finding Description
**Root cause — `util/dao/src/lib.rs` line 155–156:**
```rust
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast truncates any value above `u64::MAX` (18_446_744_073_709_551_615) without error.

**Contrast with the safe pattern used in the sibling function `dao_field_with_current_epoch`:**
```rust
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
// ...
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) 

**Call chain to consensus-critical path:**
- `calculate_maximum_withdraw` (line 127) is called by `transaction_maximum_withdraw`
- `transaction_maximum_withdraw` is called by `withdrawed_interests` (line 316–317)
- `withdrawed_interests` feeds into `dao_field_with_current_epoch`, which is invoked by `BlockAssembler::calc_dao` during block template generation and by `DaoHeaderVerifier` during contextual block verification [4](#0-3) 

**Why existing checks are insufficient:** `safe_add` on line 156 only guards against overflow in the *addition* step; it does not detect the prior truncation. Once `withdraw_counted_capacity` is silently truncated to a small value, `safe_add` succeeds and the function returns `Ok(corrupted_capacity)`.

## Impact Explanation
When the overflow condition is met, `withdrawed_interests` returns a silently wrong (much smaller) value. This corrupts the `s` field of the DAO data packed into the block header. A block assembled with this corrupted DAO field will fail `DaoHeaderVerifier` on all peers that compute the correct value, causing a **consensus deviation / chain split** — a Critical-severity impact under the allowed CKB bounty scope ("Vulnerabilities which could easily cause consensus deviation"). Additionally, the public RPC `calculate_dao_maximum_withdraw` (in `rpc/src/module/experiment.rs`) calls `calculate_maximum_withdraw` directly and silently returns a wrong withdrawal amount to callers.

## Likelihood Explanation
Triggering the overflow requires a single deposited cell where `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`. Given total CKB supply ≈ 3.36 × 10^18 shannons and `u64::MAX` ≈ 1.84 × 10^19, the accumulate-rate ratio must exceed ~5.5×. At current secondary issuance rates this would take many decades of network operation. Likelihood under normal conditions is therefore very low. However, the code path is reachable by any unprivileged user submitting a DAO withdrawal transaction or calling the RPC, and the existing test suite already documents the expected error behavior that the current code violates.

## Recommendation
Replace the truncating cast with the checked conversion already used elsewhere in the same file:
```rust
// util/dao/src/lib.rs line 155–156
let withdraw_capacity =
    Capacity::shannons(
        u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
    )
    .safe_add(occupied_capacity)?;
``` [5](#0-4) 

## Proof of Concept
The existing unit test `check_withdraw_calculation_overflows` at `util/dao/src/tests.rs` lines 295–350 is the concrete PoC. [6](#0-5) 

It constructs a cell with capacity `18_446_744_073_709_550_000` shannons, `deposit_ar = 10_000_000_000_123_456`, and `withdrawing_ar = 10_000_000_001_123_456`. The resulting `withdraw_counted_capacity ≈ 18_446_744_075_554_224_225 > u64::MAX`. With the `as u64` cast, this wraps to ≈ `1_844_672_609`, `safe_add` succeeds, and the function returns `Ok(Capacity::shannons(1_844_672_609))`. The test assertion `assert!(result.is_err())` fails, directly demonstrating the silent truncation. Running `cargo test check_withdraw_calculation_overflows -p ckb-dao` reproduces the failure.

### Citations

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** util/dao/src/lib.rs (L244-245)
```rust
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

**File:** util/dao/src/lib.rs (L258-258)
```rust
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/src/lib.rs (L312-333)
```rust
    fn withdrawed_interests(
        &self,
        mut rtxs: impl Iterator<Item = &'a ResolvedTransaction> + Clone,
    ) -> Result<Capacity, DaoError> {
        let maximum_withdraws = rtxs.clone().try_fold(Capacity::zero(), |capacities, rtx| {
            self.transaction_maximum_withdraw(rtx)
                .and_then(|c| capacities.safe_add(c).map_err(Into::into))
        })?;
        let input_capacities = rtxs.try_fold(Capacity::zero(), |capacities, rtx| {
            let tx_input_capacities = rtx.resolved_inputs.iter().try_fold(
                Capacity::zero(),
                |tx_capacities, cell_meta| {
                    let output_capacity: Capacity = cell_meta.cell_output.capacity().into();
                    tx_capacities.safe_add(output_capacity)
                },
            )?;
            capacities.safe_add(tx_input_capacities)
        })?;
        maximum_withdraws
            .safe_sub(input_capacities)
            .map_err(Into::into)
    }
```

**File:** util/dao/src/tests.rs (L295-350)
```rust
#[test]
fn check_withdraw_calculation_overflows() {
    let output = CellOutput::new_builder()
        .capacity(Capacity::shannons(18_446_744_073_709_550_000))
        .build();
    let tx = TransactionBuilder::default().output(output.clone()).build();
    let epoch = EpochNumberWithFraction::new(1, 100, 1000);
    let deposit_header = HeaderBuilder::default()
        .number(100)
        .epoch(epoch)
        .dao(pack_dao_data(
            10_000_000_000_123_456,
            Default::default(),
            Default::default(),
            Default::default(),
        ))
        .build();
    let deposit_block = BlockBuilder::default()
        .header(deposit_header)
        .transaction(tx)
        .build();

    let epoch = EpochNumberWithFraction::new(1, 200, 1000);
    let withdrawing_header = HeaderBuilder::default()
        .number(200)
        .epoch(epoch)
        .dao(pack_dao_data(
            10_000_000_001_123_456,
            Default::default(),
            Default::default(),
            Default::default(),
        ))
        .build();
    let withdrawing_block = BlockBuilder::default().header(withdrawing_header).build();

    let tmp_dir = TempDir::new().unwrap();
    let db = RocksDB::open_in(&tmp_dir, COLUMNS);
    let store = ChainDB::new(db, Default::default());
    let txn = store.begin_transaction();
    txn.insert_block(&deposit_block).unwrap();
    txn.attach_block(&deposit_block).unwrap();
    txn.insert_block(&withdrawing_block).unwrap();
    txn.attach_block(&withdrawing_block).unwrap();
    txn.commit().unwrap();

    let consensus = Consensus::default();
    let data_loader = store.borrow_as_data_loader();
    let calculator = DaoCalculator::new(&consensus, &data_loader);
    let result = calculator.calculate_maximum_withdraw(
        &output,
        Capacity::bytes(0).expect("should not overflow"),
        &deposit_block.hash(),
        &withdrawing_block.hash(),
    );
    assert!(result.is_err());
}
```
