### Title
Silent u128→u64 Truncation in `DaoCalculator::calculate_maximum_withdraw` Produces Wrong NervosDAO Withdrawal Amount — (File: `util/dao/src/lib.rs`)

---

### Summary

In `DaoCalculator::calculate_maximum_withdraw`, the intermediate u128 result `withdraw_counted_capacity` is cast to u64 with a silent truncating `as u64` cast. If the product `counted_capacity * withdrawing_ar / deposit_ar` exceeds `u64::MAX`, the high bits are silently discarded, producing a drastically wrong (much smaller) withdrawal capacity. This is structurally analogous to the BloomPool bug: an arithmetic scaling step is handled incorrectly, causing the final financial value to be silently mis-computed. Every other u128→u64 conversion in the same file uses `u64::try_from(...)` to detect overflow; only this one uses the silent `as u64` cast.

---

### Finding Description

In `util/dao/src/lib.rs` lines 152–156:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

The `as u64` cast on line 156 silently truncates if `withdraw_counted_capacity > u64::MAX`. Compare with every other analogous u128→u64 conversion in the same file:

- `ar_increase128` → `u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?` (line 258)
- `miner_issuance128` → `u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?` (lines 244–245)
- `reward128` in `secondary_block_reward` → `u64::try_from(reward128).map_err(|_| DaoError::Overflow)?` (line 204)

All three use `try_from` to propagate a `DaoError::Overflow`. Only `calculate_maximum_withdraw` uses the silent `as u64` cast, creating an inconsistency that allows the overflow to go undetected. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

---

### Impact Explanation

If `withdraw_counted_capacity` overflows u64 and is silently truncated:

1. **RPC path** (`calculate_dao_maximum_withdraw`): The RPC returns a drastically wrong (much smaller) withdrawal capacity. A user who relies on this RPC to construct their phase-2 withdrawal transaction will claim far less than their entitled amount — a direct financial loss.

2. **Transaction verification path** (`transaction_maximum_withdraw` → `calculate_maximum_withdraw` → `transaction_fee`): The node computes a smaller `maximum_withdraw` than the actual entitled amount. A user who constructs a withdrawal transaction claiming the correct (larger) amount will have their transaction rejected with a fee underflow error, locking them out of their NervosDAO deposit.

The truncation produces a value that is `withdraw_counted_capacity mod 2^64`, which can be orders of magnitude smaller than the correct value, not a small rounding error. [5](#0-4) [6](#0-5) 

---

### Likelihood Explanation

**Low.** The total CKB supply is approximately 33.6 billion CKB = 3.36×10¹⁸ shannons, well below `u64::MAX` ≈ 18.4×10¹⁸ shannons. For `withdraw_counted_capacity` to exceed `u64::MAX`, the accumulate-rate ratio `withdrawing_ar / deposit_ar` must exceed approximately 5.5. The genesis `ar` is `DEFAULT_GENESIS_ACCUMULATE_RATE = 10^16`; it grows by `ar * g2 / C` per block. At the current secondary issuance rate (~4% annual growth), the ar would need approximately 43 years to grow 5.5×. The code defect is real and demonstrable, and is inconsistent with every other analogous calculation in the same file, but practical exploitation requires an extreme time horizon. [7](#0-6) 

---

### Recommendation

Replace the silent `as u64` cast with `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, consistent with all other u128→u64 conversions in the same file:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
``` [8](#0-7) 

---

### Proof of Concept

```rust
// Hypothetical scenario: ar has grown 6x since deposit (theoretically possible after ~43 years)
let deposit_ar: u64    = 10_000_000_000_000_000; // genesis ar = 10^16
let withdrawing_ar: u64 = 60_000_000_000_000_000; // ar grew 6x
let counted_capacity: u64 = 3_360_000_000_000_000_000; // ~33.6B CKB in shannons

let withdraw_counted_capacity: u128 = u128::from(counted_capacity)
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
// = 3.36e18 * 6e16 / 1e16 = 3.36e18 * 6 = 2.016e19
// u64::MAX                              = 1.844e19
// 2.016e19 > u64::MAX → overflow!

let truncated = withdraw_counted_capacity as u64;
// = 2.016e19 mod 2^64 ≈ 1.72e18  (wrong — should be 2.016e19)
// User receives ~1.72e18 shannons instead of ~2.016e19 shannons
// Loss: ~18.44 billion CKB worth of interest
```

The existing test `check_withdraw_calculation_overflows` catches only the downstream `safe_add` overflow (where the truncated value plus `occupied_capacity` exceeds u64::MAX), not the silent truncation itself. There is no test that verifies the truncation path produces an error rather than a silently wrong result. [9](#0-8)

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

**File:** util/dao/src/lib.rs (L202-204)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/src/lib.rs (L242-245)
```rust
        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
```

**File:** util/dao/src/lib.rs (L256-258)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
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

**File:** util/dao/utils/src/lib.rs (L16-17)
```rust
// This is multiplied by 10**16 to make sure we have enough precision.
const DEFAULT_GENESIS_ACCUMULATE_RATE: u64 = 10_000_000_000_000_000;
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
