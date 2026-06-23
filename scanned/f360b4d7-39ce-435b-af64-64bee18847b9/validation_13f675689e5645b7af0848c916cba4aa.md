### Title
Silent u128→u64 Truncation in DAO Withdrawal Capacity Calculation Silently Destroys Depositor Funds — (`File: util/dao/src/lib.rs`)

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the interest-adjusted withdrawal capacity using a `u128` intermediate value but narrows it back to `u64` with a bare `as u64` cast. This is a silent truncating cast: if the intermediate value exceeds `u64::MAX`, the result wraps to a tiny number with no error, silently destroying the depositor's interest and principal in the accounting. Every other analogous u128→u64 narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, making this omission a clear inconsistency.

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
// Lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast on line 156 silently truncates `withdraw_counted_capacity` modulo `2^64` if it exceeds `u64::MAX`. No error is raised; the function proceeds with a drastically understated capacity value.

Every other u128→u64 narrowing in the same file uses the checked form:

- `secondary_block_reward` (line 204): `let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;` [2](#0-1) 

- `dao_field_with_current_epoch` (line 245): `Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)` [3](#0-2) 

- `dao_field_with_current_epoch` (line 258): `let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;` [4](#0-3) 

`calculate_maximum_withdraw` is the sole outlier.

### Impact Explanation

`calculate_maximum_withdraw` is called from two critical paths:

**1. `transaction_maximum_withdraw` → `transaction_fee`** (lines 30–36): [5](#0-4) 

Used by the contextual block verifier to compute the fee for DAO withdrawal transactions. If `withdraw_counted_capacity` silently wraps to a tiny value, `maximum_withdraw` becomes far smaller than the actual input capacity. The subsequent `safe_sub(outputs_capacity)` underflows and returns `DaoError::Overflow`, causing the node to reject the DAO withdrawal block — permanently locking the depositor's funds.

**2. `transaction_maximum_withdraw` → `withdrawed_interests` → `dao_field_with_current_epoch`** (lines 312–333): [6](#0-5) 

Used to update the DAO state field `current_s` (secondary issuance accumulator). A truncated `withdrawed_interests` causes `current_s` to be computed too large (less interest is subtracted), corrupting the DAO state for all future depositors. [7](#0-6) 

The existing overflow test `check_withdraw_calculation_overflows` only exercises the `safe_add` overflow path (where `withdraw_counted_capacity + occupied_capacity > u64::MAX`), not the silent `as u64` truncation path (where `withdraw_counted_capacity > u64::MAX` alone). [8](#0-7) 

### Likelihood Explanation

For the truncation to trigger, `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`. Since `deposit_ar` starts at `10^16` and the total CKB supply is ~3.36 × 10^18 shannons, the ratio `withdrawing_ar / deposit_ar` would need to exceed ~5.5×, which at the DAO interest rate of ~3–5% per year would take decades on mainnet. Likelihood on mainnet is therefore very low. On testnets or devnets with modified genesis parameters (larger initial supply or higher secondary issuance), the threshold is reachable much sooner. The bug is real, the code path is reachable by any NervosDAO depositor (an unprivileged transaction sender), and the inconsistency with the rest of the file confirms it is unintentional.

### Recommendation

Replace the bare `as u64` cast with the same checked conversion used everywhere else in the file:

```rust
// Before (silent truncation):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (consistent with rest of file):
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?
```

Add a unit test that sets `counted_capacity * withdrawing_ar / deposit_ar` to exactly `u64::MAX + 1` and asserts `DaoError::Overflow` is returned, covering the currently untested truncation path.

### Proof of Concept

```
deposit_ar      = 10_000_000_000_000_001   (just above genesis value)
withdrawing_ar  = 18_446_744_073_709_551_616 * deposit_ar / counted_capacity + 1
                  (chosen so that counted_capacity * withdrawing_ar / deposit_ar = u64::MAX + 1)
counted_capacity = any value close to u64::MAX (achievable on devnet)

Result of `withdraw_counted_capacity as u64`:
  (u64::MAX + 1) as u64 = 0   ← silent wrap

withdraw_capacity = Capacity::shannons(0).safe_add(occupied_capacity)
                  = occupied_capacity   ← depositor receives only the cell's minimum rent,
                                          all interest and principal are silently destroyed
```

The `transaction_fee` call then computes `0 + occupied_capacity - outputs_capacity`, which underflows and returns `DaoError::Overflow`, causing the withdrawal block to be rejected and the depositor's funds to be permanently locked.

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

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** util/dao/src/lib.rs (L202-204)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/src/lib.rs (L244-245)
```rust
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

**File:** util/dao/src/lib.rs (L252-254)
```rust
        let current_s = parent_s
            .safe_add(nervosdao_issuance)
            .and_then(|s| s.safe_sub(withdrawed_interests))?;
```

**File:** util/dao/src/lib.rs (L256-258)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
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
