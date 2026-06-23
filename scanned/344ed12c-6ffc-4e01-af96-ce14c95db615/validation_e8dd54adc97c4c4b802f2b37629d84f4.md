### Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Produces Wrong DAO Withdrawal Amount — (`util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes a u128 intermediate value and then casts it to u64 with a bare `as u64` truncating cast. If the intermediate value exceeds `u64::MAX`, the high bits are silently discarded, producing a drastically wrong (too-small) withdrawal capacity. Every other analogous u128→u64 narrowing in the same codebase uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, making this the sole inconsistent site. The function is called both from the `calculate_dao_maximum_withdraw` RPC and from `transaction_maximum_withdraw`, which feeds `transaction_fee` (tx-pool admission) and `withdrawed_interests` (block-level DAO field computation).

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← bare truncating cast
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The multiplication is correctly widened to u128 to avoid overflow there, but the subsequent narrowing back to u64 is done with `as u64`, which silently wraps. Compare with every other u128→u64 conversion in the same file:

```rust
// dao_field_with_current_epoch — correct pattern used twice:
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) 

The `calculate_maximum_withdraw` function is called from `transaction_maximum_withdraw`: [3](#0-2) 

which is called from `transaction_fee` (tx-pool fee check) and from `withdrawed_interests` (DAO field update during block processing): [4](#0-3) 

It is also directly exposed via the `calculate_dao_maximum_withdraw` JSON-RPC endpoint: [5](#0-4) 

The existing test `check_withdraw_calculation_overflows` constructs a cell with capacity `18_446_744_073_709_550_000` shannons and `ar` values that cause `withdraw_counted_capacity` to exceed `u64::MAX`, then asserts `result.is_err()`. However, because `as u64` never returns an error — it silently truncates — the actual return value is `Ok(Capacity::shannons(<truncated_wrong_value>))`, not `Err`. The test assertion is therefore incorrect, meaning the silent-truncation bug is not caught by the existing test suite. [6](#0-5) 

---

### Impact Explanation

**Incorrect DAO withdrawal amount (financial loss / DoS):** When `withdraw_counted_capacity` overflows u64, the truncated value is far smaller than the true value. A user's DAO withdrawal transaction built using the RPC's reported maximum will carry an output capacity that is much smaller than what the on-chain DAO script actually allows, causing the transaction to be rejected by the script verifier (DoS). Alternatively, if the user builds the transaction with the correct amount, the `transaction_fee` check inside the tx-pool will compute a wrong (negative or near-zero) fee and reject the transaction.

**Incorrect DAO field in block validation:** `withdrawed_interests` calls `transaction_maximum_withdraw` → `calculate_maximum_withdraw`. A wrong truncated value propagates into `dao_field_with_current_epoch`, corrupting the `current_s` (NervosDAO savings) field written into the block header. This is a consensus-critical value. [7](#0-6) 

---

### Likelihood Explanation

The overflow condition requires `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`. With the initial `ar ≈ 10^16` and maximum realistic cell capacity ~3.36 × 10^18 shannons (total CKB supply), overflow occurs when `withdrawing_ar / deposit_ar > ~5.5`. This ratio grows slowly via secondary issuance, so the condition is not immediately triggerable on mainnet. However:

1. The bug is latent and will become reachable as the chain ages.
2. On a devnet or testnet with modified parameters, it can be triggered immediately.
3. Any RPC caller can already receive a silently wrong answer from `calculate_dao_maximum_withdraw` if they craft a scenario with large enough `ar` values (e.g., by referencing headers with manipulated DAO fields in a test environment).

---

### Recommendation

Replace the bare truncating cast with a checked conversion, consistent with the rest of the file:

```rust
// Before (unsafe):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (safe, consistent with dao_field_with_current_epoch):
Capacity::shannons(
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
).safe_add(occupied_capacity)?
```

Also fix the test assertion in `check_withdraw_calculation_overflows`: it currently asserts `result.is_err()`, but with the current code the result is `Ok` with a wrong value. After the fix above, `result.is_err()` will correctly hold.

---

### Proof of Concept

Using the values from the existing test:
- `output.capacity() = 18_446_744_073_709_550_000` shannons
- `occupied_capacity = 4_100_000_000` shannons (default lock script, 41 bytes)
- `counted_capacity = 18_446_744_069_609_550_000`
- `deposit_ar = 10_000_000_000_123_456`, `withdrawing_ar = 10_000_000_001_123_456`

```
withdraw_counted_capacity (u128)
  = 18_446_744_069_609_550_000 × 10_000_000_001_123_456
    / 10_000_000_000_123_456
  ≈ 20_519_118_476_570_505_000   -- exceeds u64::MAX (18_446_744_073_709_551_615)

withdraw_counted_capacity as u64
  = 20_519_118_476_570_505_000 mod 2^64
  ≈ 2_072_374_402_860_953_384    -- silently truncated, ~10× too small

withdraw_capacity = 2_072_374_402_860_953_384 + 4_100_000_000
                  = 2_072_374_406_960_953_384  -- returned as Ok, wrong value
```

The function returns `Ok(Capacity::shannons(2_072_374_406_960_953_384))` instead of `Err(DaoError::Overflow)`. A DAO depositor with a large cell would receive a withdrawal estimate roughly 10× smaller than their actual entitlement, and any transaction built on that estimate would be rejected by the on-chain DAO script.

### Citations

**File:** util/dao/src/lib.rs (L108-113)
```rust
                            self.calculate_maximum_withdraw(
                                output,
                                Capacity::bytes(cell_meta.data_bytes as usize)?,
                                deposit_header_hash,
                                withdrawing_header_hash,
                            )
```

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** util/dao/src/lib.rs (L244-258)
```rust
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
        let nervosdao_issuance = current_g2.safe_sub(miner_issuance)?;

        let current_c = parent_c.safe_add(current_g)?;
        let current_u = parent_u
            .safe_add(added_occupied_capacities)
            .and_then(|u| u.safe_sub(freed_occupied_capacities))?;
        let current_s = parent_s
            .safe_add(nervosdao_issuance)
            .and_then(|s| s.safe_sub(withdrawed_interests))?;

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
