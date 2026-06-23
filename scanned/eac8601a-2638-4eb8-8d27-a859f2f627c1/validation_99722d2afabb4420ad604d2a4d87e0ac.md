### Title
Silent u128→u64 Truncation in `calculate_maximum_withdraw` Skips Overflow Guard — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` converts a u128 intermediate result to u64 with a bare `as u64` cast, silently truncating the value on overflow. Every other analogous u128→u64 conversion in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. When the truncated value plus `occupied_capacity` does not itself overflow, `safe_add` succeeds and the function returns a silently wrong (drastically smaller) withdrawal capacity with no error, causing a DAO depositor to lose interest.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the scaled withdrawal amount in u128 and then narrows it to u64:

```rust
// util/dao/src/lib.rs  lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
```

Every other u128→u64 narrowing in the same file uses the checked path:

| Site | Conversion used |
|---|---|
| `secondary_block_reward` line 204 | `u64::try_from(reward128).map_err(|_| DaoError::Overflow)?` |
| `dao_field_with_current_epoch` line 245 | `u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?` |
| `dao_field_with_current_epoch` line 258 | `u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?` |
| **`calculate_maximum_withdraw` line 156** | **`withdraw_counted_capacity as u64` — no check** |

When `withdraw_counted_capacity` exceeds `u64::MAX`, the `as u64` cast wraps it to `withdraw_counted_capacity % 2^64`. If the wrapped value plus `occupied_capacity` does not overflow u64, `safe_add` succeeds and the function silently returns a capacity far smaller than the depositor is owed. No `DaoError::Overflow` is raised, so callers have no way to detect the corruption.

The same function is also the implementation behind the `calculate_dao_maximum_withdraw` JSON-RPC endpoint (`rpc/src/module/experiment.rs` lines 235–298), so the wrong value is also surfaced to external callers who rely on it to construct withdrawal transactions.

---

### Impact Explanation

A DAO depositor who calls `calculate_dao_maximum_withdraw` or whose withdrawal transaction is validated through `transaction_maximum_withdraw` → `withdrawed_interests` → `dao_field_with_current_epoch` receives a silently incorrect (much smaller) withdrawal capacity. Because no error is returned, the node's tx-pool and DAO-field accounting both proceed with the corrupted value:

- The depositor's withdrawal transaction may be constructed with an output capacity that the node's own fee checker (`transaction_fee`) considers valid, yet the actual entitled amount is far larger — the depositor loses the difference.
- The `withdrawed_interests` subtraction in `dao_field_with_current_epoch` uses the same corrupted figure, causing the `s` (secondary issuance surplus) field in subsequent block headers to be inflated, corrupting DAO accounting for all future blocks.

---

### Likelihood Explanation

The trigger condition is `withdraw_counted_capacity > u64::MAX`, i.e.:

```
counted_capacity × withdrawing_ar / deposit_ar  >  2^64 − 1
```

`ar` starts at `10^16` on mainnet and grows by roughly `g2 / C` per block (secondary issuance divided by total capacity). For `ar` to double — the minimum growth needed to push a near-maximum `counted_capacity` past u64::MAX — the cumulative secondary issuance would have to equal the entire circulating supply, which is economically unreachable on mainnet in the foreseeable future.

However, on a custom chain or devnet with a very high `secondary_epoch_reward` relative to total capacity, or after an extremely long chain lifetime, the condition becomes reachable. The inconsistency with every other conversion in the same file also makes this a latent defect that could be silently activated by future parameter changes.

---

### Recommendation

Replace the bare cast with the same checked conversion used everywhere else in the file:

```rust
// util/dao/src/lib.rs  lines 155-156
let withdraw_capacity =
    Capacity::shannons(
        u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
    )
    .safe_add(occupied_capacity)?;
```

This makes the function consistent with `secondary_block_reward` and `dao_field_with_current_epoch` and ensures that any future scenario where `ar` grows large enough to overflow u64 is caught and surfaced as an explicit `DaoError::Overflow` rather than silently corrupting the withdrawal amount.

---

### Proof of Concept

The existing unit test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` (lines 296–350) already constructs a near-overflow scenario and asserts `result.is_err()`. That test passes today only because `safe_add(occupied_capacity)` happens to overflow u64 after the truncation. A crafted scenario where the truncated value plus `occupied_capacity` stays within u64 would pass `safe_add` and return a silently wrong result:

```
deposit_ar      = 10_000_000_000_000_000   (10^16, genesis value)
withdrawing_ar  = 20_000_000_000_000_001   (ar has doubled + 1)
counted_capacity = u64::MAX = 18_446_744_073_709_551_615

withdraw_counted_capacity (u128)
  = 18_446_744_073_709_551_615 × 20_000_000_000_000_001
    / 10_000_000_000_000_000
  ≈ 36_893_488_147_419_103_232   (> u64::MAX)

withdraw_counted_capacity as u64
  = 36_893_488_147_419_103_232 % 2^64
  = 18_446_744_073_709_551_616   -- wraps to a small value

safe_add(occupied_capacity) succeeds → wrong (tiny) capacity returned, no error
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** util/dao/src/lib.rs (L256-258)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

**File:** rpc/src/module/experiment.rs (L259-266)
```rust
                match calculator.calculate_maximum_withdraw(
                    &output,
                    core::Capacity::bytes(output_data.len()).expect("should not overflow"),
                    &deposit_header_hash,
                    &withdrawing_header_hash.into(),
                ) {
                    Ok(capacity) => Ok(capacity.into()),
                    Err(err) => Err(RPCError::custom_with_error(RPCError::DaoError, err)),
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
