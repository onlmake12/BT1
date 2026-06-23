### Title
Silent `u128 as u64` Truncation in NervosDAO Withdrawal Capacity Accounting Leads to Incorrect DAO Field - (File: util/dao/src/lib.rs)

### Summary
`DaoCalculator::calculate_maximum_withdraw` computes a 128-bit intermediate value `withdraw_counted_capacity` and then casts it to `u64` with a bare `as u64`, which silently truncates on overflow. Every other analogous calculation in the same file uses `u64::try_from(…).map_err(|_| DaoError::Overflow)?`. When the truncation fires, the function returns a silently wrong (too-small) withdrawal amount, which propagates into `withdrawed_interests` and causes the DAO header field `current_s` (NervosDAO savings) to be over-counted — recording more savings than actually exist.

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast is a **silent truncation**: if `withdraw_counted_capacity > u64::MAX`, the high bits are discarded and the function returns `Ok(some_wrong_small_value)` instead of `Err(DaoError::Overflow)`.

Every other u128→u64 narrowing in the same file uses the checked form:

```rust
// secondary_block_reward (line 204)
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;

// dao_field_with_current_epoch (line 244-245)
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
``` [2](#0-1) [3](#0-2) 

The inconsistency is a clear defect. The existing test `check_withdraw_calculation_overflows` does not catch this: it uses a capacity so large that the subsequent `safe_add(occupied_capacity)` overflows, masking the silent truncation path. [4](#0-3) 

### Impact Explanation

`calculate_maximum_withdraw` feeds into two call chains:

1. **DAO field accounting** (`withdrawed_interests` → `dao_field_with_current_epoch`):

```rust
let current_s = parent_s
    .safe_add(nervosdao_issuance)
    .and_then(|s| s.safe_sub(withdrawed_interests))?;
``` [5](#0-4) 

If `calculate_maximum_withdraw` returns a truncated (too-small) value, `withdrawed_interests` is too small, so `current_s` is **over-counted**. The DAO header field records more NervosDAO savings than actually exist. This is the direct analog to the original report's insolvency: the accounting records a balance in excess of the real balance.

2. **Transaction fee validation** (`transaction_fee`): a truncated `maximum_withdraw` makes the computed fee appear smaller than it is, causing valid large-deposit withdrawal transactions to be rejected as having insufficient fees — a denial-of-service for affected depositors. [6](#0-5) 

The incorrect `current_s` is committed to the chain via `DaoHeaderVerifier`, which compares the block header's DAO field against the node's computed value: [7](#0-6) 

Because both the miner and the validator run the same buggy code, the wrong DAO field is accepted by consensus, permanently corrupting the on-chain NervosDAO savings accounting.

### Likelihood Explanation

For `withdraw_counted_capacity` to exceed `u64::MAX`:

```
counted_capacity × withdrawing_ar / deposit_ar > 2^64 − 1
```

Since `withdrawing_ar / deposit_ar` is always slightly above 1 (AR grows slowly), `counted_capacity` must itself be close to `u64::MAX` (≈ 18.4 billion CKB in shannons). The total CKB supply is ~33.6 billion CKB, so a single cell holding ~18+ billion CKB is theoretically possible (e.g., a large institutional deposit). The likelihood is **low but non-zero**, and the impact when triggered is permanent on-chain accounting corruption.

### Recommendation

Replace the silent cast with the checked conversion already used elsewhere in the same file:

```rust
// Before (silent truncation):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (consistent with the rest of the file):
Capacity::shannons(
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
).safe_add(occupied_capacity)?
``` [8](#0-7) 

Also update `check_withdraw_calculation_overflows` to use a capacity value that specifically exercises the `as u64` truncation path (where `withdraw_counted_capacity` wraps to a small value that does **not** cause `safe_add` to overflow), confirming the fix returns `Err(DaoError::Overflow)` in that case.

### Proof of Concept

Construct a DAO deposit cell with `capacity = u64::MAX - 1` shannons and `occupied_capacity = 0`. Choose `withdrawing_ar` and `deposit_ar` such that:

```
(u64::MAX - 1) × withdrawing_ar / deposit_ar = u64::MAX + k   (k small, e.g. 229)
```

With the current code, `withdraw_counted_capacity as u64 = k - 1 = 228`. `safe_add(0)` succeeds, and `calculate_maximum_withdraw` returns `Ok(Capacity::shannons(228))` — a value ~18.4 billion CKB less than the correct answer — with no error. The miner includes a withdrawal transaction using this wrong amount; both miner and validator compute the same wrong `withdrawed_interests`; the block is accepted; `current_s` is permanently inflated by the difference. [9](#0-8)

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

**File:** util/dao/src/lib.rs (L202-205)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
        Ok(Capacity::shannons(reward))
```

**File:** util/dao/src/lib.rs (L242-246)
```rust
        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
        let nervosdao_issuance = current_g2.safe_sub(miner_issuance)?;
```

**File:** util/dao/src/lib.rs (L252-254)
```rust
        let current_s = parent_s
            .safe_add(nervosdao_issuance)
            .and_then(|s| s.safe_sub(withdrawed_interests))?;
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

**File:** verification/contextual/src/contextual_block_verifier.rs (L300-320)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let dao = DaoCalculator::new(
            &self.context.consensus,
            &self.context.store.borrow_as_data_loader(),
        )
        .dao_field(self.resolved.iter().map(AsRef::as_ref), self.parent)
        .map_err(|e| {
            error_target!(
                crate::LOG_TARGET,
                "Error generating dao data for block {}: {:?}",
                self.header.hash(),
                e
            );
            e
        })?;

        if dao != self.header.dao() {
            return Err((BlockErrorKind::InvalidDAO).into());
        }
        Ok(())
    }
```
