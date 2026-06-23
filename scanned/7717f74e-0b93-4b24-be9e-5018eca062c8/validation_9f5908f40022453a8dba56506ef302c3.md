### Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Produces Wrong Withdrawal Capacity — (File: `util/dao/src/lib.rs`)

---

### Summary

In `DaoCalculator::calculate_maximum_withdraw`, the intermediate `u128` result `withdraw_counted_capacity` is narrowed to `u64` via a bare `as u64` cast — a **silent truncating cast** — instead of the checked `u64::try_from(...).map_err(|_| DaoError::Overflow)?` pattern used consistently for every other analogous conversion in the same file. When `withdraw_counted_capacity` exceeds `u64::MAX`, the upper bits are silently discarded, producing a drastically wrong (much smaller) capacity value rather than returning `DaoError::Overflow`.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the maximum CKB a depositor may withdraw from NervosDAO:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast is a Rust truncating cast: if `withdraw_counted_capacity > u64::MAX`, the value wraps modulo 2⁶⁴ with no error, no panic, and no signal to the caller.

Every other `u128 → u64` narrowing in the same impl block uses the checked pattern:

```rust
// dao_field_with_current_epoch — miner issuance
u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?
// dao_field_with_current_epoch — ar increase
u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?
// secondary_block_reward
u64::try_from(reward128).map_err(|_| DaoError::Overflow)?
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The inconsistency is structural: one conversion was written with `as u64` while all siblings use `try_from`.

The existing overflow test (`check_withdraw_calculation_overflows`) passes only because, for its specific inputs, the overflow is caught downstream by `safe_add(occupied_capacity)` — not by the `as u64` cast. For inputs where the truncated value is small enough that `safe_add` does not overflow, the function silently returns `Ok(wrong_capacity)` instead of `Err(DaoError::Overflow)`. [5](#0-4) 

---

### Impact Explanation

`calculate_maximum_withdraw` feeds into two consensus-critical paths:

1. **`withdrawed_interests`** → **`dao_field_with_current_epoch`**: The DAO field is embedded in every block header. A node that computes a wrong `withdrawed_interests` (because `calculate_maximum_withdraw` returned a silently truncated value) will pack a wrong DAO field into the block it mines or will reject a valid block from a peer whose DAO field is correct. Either outcome is a **consensus split**.

2. **`transaction_fee`**: Used by the tx-pool to compute the fee for a DAO withdrawal transaction. A wrong fee could cause valid transactions to be mispriced or rejected. [6](#0-5) [7](#0-6) 

---

### Likelihood Explanation

For `withdraw_counted_capacity` to exceed `u64::MAX`:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

Because `withdrawing_ar ≥ deposit_ar` (the accumulation rate only grows), the ratio `withdrawing_ar / deposit_ar` must be meaningfully greater than 1. The genesis accumulation rate is `10_000_000_000_000_000` (10¹⁶) and grows by roughly `~10⁴` per block. For the ratio to reach 1.0025 (the minimum needed to overflow a cell holding the entire circulating supply of ~3.36 × 10¹⁸ shannons) would require on the order of hundreds of millions of blocks — far beyond any realistic deposit/withdrawal window.

Additionally, no single cell can hold more CKB than the total issuance, which is well below `u64::MAX` shannons. This makes the overflow practically unreachable on mainnet under normal economic conditions.

**Likelihood: Very Low.** The bug is real and the code is inconsistent, but triggering it requires a cell capacity approaching `u64::MAX` shannons combined with a very large `ar` ratio growth — conditions that cannot occur within the economic constraints of the live network.

---

### Recommendation

Replace the silent cast with the checked conversion already used everywhere else in the file:

```rust
// Before (silent truncation):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (consistent with the rest of the file):
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?
```

This makes the overflow handling consistent with `miner_issuance128`, `ar_increase128`, and `reward128` conversions in the same crate, and ensures the existing test `check_withdraw_calculation_overflows` exercises the correct error path.

---

### Proof of Concept

The inconsistency is directly visible by comparing line 156 against lines 244–245, 258, and 204 in `util/dao/src/lib.rs`.

A concrete numeric demonstration of the truncation path:

- `counted_capacity` = `u64::MAX - 1` = `18_446_744_073_709_551_614`
- `deposit_ar` = `10_000_000_000_000_000`
- `withdrawing_ar` = `10_000_000_000_000_000 * 2` = `20_000_000_000_000_000` (hypothetical doubled rate)

```
withdraw_counted_capacity (u128)
  = 18_446_744_073_709_551_614 × 20_000_000_000_000_000
    / 10_000_000_000_000_000
  = 36_893_488_147_419_103_228   -- exceeds u64::MAX (18_446_744_073_709_551_615)

withdraw_counted_capacity as u64
  = 36_893_488_147_419_103_228 mod 2^64
  = 36_893_488_147_419_103_228 - 18_446_744_073_709_551_616
  = 18_446_744_073_709_551_612   -- silently wrong, no error returned
```

The function then returns `Ok(Capacity::shannons(18_446_744_073_709_551_612 + occupied_capacity))` — a value that may itself overflow `safe_add` or may not, depending on `occupied_capacity` — instead of the correct `Err(DaoError::Overflow)`. [8](#0-7)

### Citations

**File:** util/dao/src/lib.rs (L30-36)
```rust
    pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
        let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
        rtx.transaction
            .outputs_capacity()
            .and_then(|y| maximum_withdraw.safe_sub(y))
            .map_err(Into::into)
    }
```

**File:** util/dao/src/lib.rs (L149-158)
```rust
        let occupied_capacity = output.occupied_capacity(output_data_capacity)?;
        let output_capacity: Capacity = output.capacity().into();
        let counted_capacity = output_capacity.safe_sub(occupied_capacity)?;
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

        Ok(withdraw_capacity)
```

**File:** util/dao/src/lib.rs (L204-204)
```rust
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
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

**File:** util/dao/src/tests.rs (L296-349)
```rust
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
```
