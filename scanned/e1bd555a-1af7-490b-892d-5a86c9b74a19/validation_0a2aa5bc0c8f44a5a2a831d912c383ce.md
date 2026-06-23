### Title
Silent u128→u64 Truncating Cast in `DaoCalculator::calculate_maximum_withdraw` Silently Corrupts DAO Withdrawal Accounting — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the scaled withdrawal capacity as a `u128` intermediate value and then narrows it to `u64` using a bare `as u64` cast — a silent, truncating operation in Rust. Every other u128→u64 narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, which surfaces the overflow as a `DaoError`. The inconsistent cast means that when the intermediate product exceeds `u64::MAX`, the high bits are silently discarded, the function returns a drastically wrong (much smaller) capacity without any error, and the corruption propagates into the global DAO field written into every subsequent block header.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

`withdraw_counted_capacity` is a `u128`. The `as u64` cast in Rust is defined to truncate: it discards the upper 64 bits without panicking or returning an error. If the value exceeds `u64::MAX`, the returned capacity is `(true_value % 2^64) + occupied_capacity` — a value that can be orders of magnitude smaller than the correct withdrawal amount.

Every other u128→u64 narrowing in the same file is done correctly:

```rust
// secondary_block_reward (line 204)
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;

// dao_field_with_current_epoch (line 244-245)
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);

// dao_field_with_current_epoch (line 258)
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The `calculate_maximum_withdraw` function is called from three distinct paths:

1. **`transaction_maximum_withdraw` → `withdrawed_interests` → `dao_field_with_current_epoch`**: The DAO field (`S_i`, the secondary issuance pool) written into every block header is derived from `withdrawed_interests`. A truncated result makes `withdrawed_interests` return a smaller-than-correct value, so `current_s` is inflated — the pool is not properly debited for the withdrawn interest. This corrupts the global DAO accounting state for all future participants. [5](#0-4) [6](#0-5) 

2. **`transaction_maximum_withdraw` → `transaction_fee`**: Fee calculation for DAO withdrawal transactions is wrong, causing the node to misclassify valid transactions as having negative fees (rejecting them) or to accept transactions that should be rejected. [7](#0-6) 

3. **RPC `calculate_dao_maximum_withdraw`**: The public RPC endpoint directly calls `calculate_maximum_withdraw` and returns the truncated value to any caller. [8](#0-7) 

---

### Impact Explanation

If `withdraw_counted_capacity` silently wraps:

- The DAO field `S_i` in block headers is permanently inflated from that block onward. All subsequent DAO depositors accrue interest against a corrupted accumulator ratio, receiving more or less interest than they are entitled to — an unfair redistribution analogous to the Yearn vault's loss-concealment issue.
- The `transaction_fee` for DAO withdrawal transactions is computed incorrectly. The node may reject valid withdrawal transactions (computed fee appears negative) or accept ones it should not.
- The RPC `calculate_dao_maximum_withdraw` silently returns a wrong value to any caller, misleading wallets and users about their entitled withdrawal amount.

Because all nodes run the same code, the corrupted DAO field would be accepted by the entire network, making the corruption consensus-level and irreversible without a hard fork.

---

### Likelihood Explanation

The truncation requires:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

`counted_capacity` is bounded by the total CKB supply (~33.6 billion CKB ≈ 3.36 × 10¹⁸ shannons, well below `u64::MAX` ≈ 1.84 × 10¹⁹ shannons). For the product to overflow `u64`, the AR ratio `withdrawing_ar / deposit_ar` must exceed approximately `1.84 × 10¹⁹ / 3.36 × 10¹⁸ ≈ 5.5×`. Given that AR grows proportionally to secondary issuance divided by total capacity, reaching a 5.5× multiple of the genesis AR would take an astronomically long time under current tokenomics.

**Likelihood is low in practice**, but the bug is a demonstrable coding defect: the same file applies `u64::try_from` with overflow checking in three analogous places and omits it in exactly this one. The existing test `check_withdraw_calculation_overflows` asserts `result.is_err()` for a near-overflow input, but the error it catches comes from the downstream `safe_add` on `occupied_capacity`, not from the `as u64` cast itself — meaning the test does not cover the silent-truncation path where `withdraw_counted_capacity as u64` wraps to a small value and `safe_add` succeeds. [9](#0-8) 

---

### Recommendation

Replace the bare `as u64` cast with the same checked conversion used everywhere else in the file:

```rust
// Before (silent truncation):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (consistent with the rest of the file):
Capacity::shannons(
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
).safe_add(occupied_capacity)?
```

Add a dedicated unit test that constructs a scenario where `withdraw_counted_capacity` itself (before `safe_add`) exceeds `u64::MAX` and verifies that `calculate_maximum_withdraw` returns `Err(DaoError::Overflow)`.

---

### Proof of Concept

**Triggering the silent truncation path** (conceptual, not requiring mainnet):

1. Construct a DAO cell with `counted_capacity` close to `u64::MAX / k` for some small integer `k`.
2. Use a `withdrawing_ar` that is `k × deposit_ar` (achievable after sufficient chain time or in a test chain with accelerated issuance).
3. Call `DaoCalculator::calculate_maximum_withdraw` with these headers.
4. With the current `as u64` cast: `withdraw_counted_capacity` wraps to a small value (e.g., 0 or 1), `safe_add(occupied_capacity)` succeeds, and the function returns `Ok(occupied_capacity)` — a value far below the correct withdrawal amount — with no error.
5. With the correct `u64::try_from(...)`: the function returns `Err(DaoError::Overflow)`, matching the behavior of all other overflow-checked paths in the same file.

The inconsistency is directly visible by comparing line 156 against lines 204, 245, and 258 of `util/dao/src/lib.rs`: [10](#0-9) [11](#0-10) [3](#0-2) [12](#0-11)

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

**File:** util/dao/src/lib.rs (L202-205)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
        Ok(Capacity::shannons(reward))
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

**File:** rpc/src/module/experiment.rs (L259-267)
```rust
                match calculator.calculate_maximum_withdraw(
                    &output,
                    core::Capacity::bytes(output_data.len()).expect("should not overflow"),
                    &deposit_header_hash,
                    &withdrawing_header_hash.into(),
                ) {
                    Ok(capacity) => Ok(capacity.into()),
                    Err(err) => Err(RPCError::custom_with_error(RPCError::DaoError, err)),
                }
```

**File:** util/dao/src/tests.rs (L296-350)
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
}
```
