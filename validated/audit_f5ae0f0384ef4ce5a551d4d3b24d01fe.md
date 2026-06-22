### Title
Silent Truncating Cast in DAO Withdrawal Capacity Calculation Bypasses Overflow Guard — (File: `util/dao/src/lib.rs`)

### Summary

`DaoCalculator::calculate_maximum_withdraw` uses a bare `as u64` truncating cast to narrow a `u128` intermediate result back to `u64`. Every other analogous `u128 → u64` narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, which propagates a checked error. The forgotten safe conversion means that if the intermediate product overflows `u64::MAX`, the value is silently truncated to a wrong (much smaller) capacity instead of returning `Err(DaoError::Overflow)`.

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the DAO withdrawal capacity as follows:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast is a Rust truncating (wrapping) cast. If `withdraw_counted_capacity` exceeds `u64::MAX`, the upper 64 bits are silently discarded, producing a drastically smaller capacity value with no error returned.

The same file contains two other `u128 → u64` narrowings that correctly use `u64::try_from`:

```rust
// dao_field_with_current_epoch — line 244-245
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
``` [2](#0-1) 

```rust
// secondary_block_reward — line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
``` [3](#0-2) 

The safe pattern (`u64::try_from`) is available and used everywhere else in the same module; it was simply forgotten in `calculate_maximum_withdraw`.

The `Capacity` type itself provides `safe_add`, `safe_sub`, and `safe_mul` precisely to prevent silent overflow: [4](#0-3) 

`calculate_maximum_withdraw` is called from `transaction_maximum_withdraw`, which feeds into `withdrawed_interests`, which in turn feeds into `dao_field_with_current_epoch` — the function that computes the DAO commitment field embedded in every block: [5](#0-4) 

It is also directly exposed via the `calculate_dao_maximum_withdraw` RPC endpoint: [6](#0-5) 

### Impact Explanation

If `withdraw_counted_capacity` (a `u128`) exceeds `u64::MAX`, the truncating cast silently produces a wrong, much smaller value. Two concrete consequences follow:

1. **Wrong DAO field in block assembly.** `withdrawed_interests` receives a silently wrong (too-small) maximum-withdraw value. This causes `current_s` (the DAO savings accumulator) in `dao_field_with_current_epoch` to be computed incorrectly. Nodes that compute the DAO field independently may disagree, causing a consensus split.

2. **Wrong RPC result.** `calculate_dao_maximum_withdraw` returns a silently wrong capacity to callers, causing DAO withdrawal transactions to be constructed with incorrect output capacities. Such transactions may be rejected by the chain or may pass with a capacity that does not match the correct interest calculation. [7](#0-6) 

### Likelihood Explanation

The overflow condition requires `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`. Since `counted_capacity ≤ u64::MAX` and the accumulation rate ratio `withdrawing_ar / deposit_ar` grows slowly on mainnet, this is unlikely to trigger under normal conditions today. However, it is a latent correctness defect: the safe-conversion pattern is already established in the codebase and was simply omitted here. Any future chain state where the AR ratio grows sufficiently (or a cell with near-maximum capacity is deposited and held for a very long time) can trigger the silent truncation. The existing unit test `check_withdraw_calculation_overflows` does not exercise the `as u64` truncation path — it only catches the subsequent `safe_add` overflow — leaving the silent truncation undetected. [8](#0-7) 

### Recommendation

Replace the truncating cast with the same checked conversion used elsewhere in the file:

```rust
// Before (unsafe):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (safe, consistent with the rest of the file):
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?
```

Add a unit test that constructs a scenario where `withdraw_counted_capacity` itself (before `safe_add`) exceeds `u64::MAX` and asserts `Err(DaoError::Overflow)` is returned.

### Proof of Concept

The root cause is at: [9](#0-8) 

Contrast with the correct pattern at: [2](#0-1) 

A minimal reproduction: set `counted_capacity` to a value near `u64::MAX` (e.g., `18_446_744_000_000_000_000`) and set `withdrawing_ar / deposit_ar` to a ratio slightly above `1.0` such that the product exceeds `u64::MAX`. With the current `as u64` cast, `calculate_maximum_withdraw` returns `Ok(wrong_small_value)`. With `u64::try_from`, it returns `Err(DaoError::Overflow)`, consistent with the behavior of `dao_field_with_current_epoch` and `secondary_block_reward` under the same conditions.

### Citations

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

**File:** util/dao/src/lib.rs (L248-254)
```rust
        let current_c = parent_c.safe_add(current_g)?;
        let current_u = parent_u
            .safe_add(added_occupied_capacities)
            .and_then(|u| u.safe_sub(freed_occupied_capacities))?;
        let current_s = parent_s
            .safe_add(nervosdao_issuance)
            .and_then(|s| s.safe_sub(withdrawed_interests))?;
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

**File:** util/occupied-capacity/core/src/units.rs (L124-130)
```rust
    /// Adds self and rhs and checks overflow error.
    pub fn safe_add<C: IntoCapacity>(self, rhs: C) -> Result<Self> {
        self.0
            .checked_add(rhs.into_capacity().0)
            .map(Capacity::shannons)
            .ok_or(Error::Overflow)
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

**File:** util/dao/src/tests.rs (L295-349)
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
```
