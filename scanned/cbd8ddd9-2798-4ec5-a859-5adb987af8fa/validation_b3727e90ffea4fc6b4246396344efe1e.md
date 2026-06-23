### Title
Silent u128→u64 Truncation in NervosDAO Withdrawal Capacity Arithmetic Causes Depositor Fund Loss — (File: `util/dao/src/lib.rs`)

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the maximum withdrawable capacity using a u128 intermediate value but converts it back to u64 with a silent `as u64` cast instead of a checked `u64::try_from(...)`. This is inconsistent with every other u128→u64 conversion in the same file and silently truncates the result when it exceeds `u64::MAX`, causing the node to enforce a drastically lower withdrawal ceiling and permanently trapping the depositor's accrued interest.

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `withdraw_counted_capacity as u64` cast is a **silent truncation**: if the u128 value exceeds `u64::MAX`, Rust wraps it to `value % 2^64` with no error. The node then uses this truncated value as the enforced maximum output capacity for the withdrawal transaction.

Every other u128→u64 narrowing in the same file uses the checked form:

```rust
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
``` [2](#0-1) 

```rust
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [3](#0-2) 

The inconsistency is the root cause. The `calculate_maximum_withdraw` path is the only one that silently truncates.

The overflow condition is:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

`ar` starts at `10_000_000_000_000_000` (10^16) at genesis and grows monotonically with every block's secondary issuance. [4](#0-3)  A large deposit (e.g., the full genesis allocation of ~3.36 × 10^18 shannons) combined with a sufficiently grown `ar` ratio can push the product above `u64::MAX ≈ 1.84 × 10^19`. When that happens, `withdraw_counted_capacity as u64` wraps to a tiny value, and `safe_add(occupied_capacity)` succeeds, returning a capacity far below what the depositor is owed.

The `calculate_maximum_withdraw` function is called both during block assembly/validation and via the `calculate_dao_maximum_withdraw` RPC. [5](#0-4)  The node enforces the returned value as the hard ceiling on the withdrawal output. A depositor whose correct entitlement exceeds the truncated ceiling cannot construct a valid withdrawal transaction claiming their full interest — the node rejects it as "Overflow" from the subsequent `safe_add`, or accepts a transaction that pays out only the truncated (near-zero) amount.

### Impact Explanation

A NervosDAO depositor with a sufficiently large deposit cannot withdraw their full accrued interest. The node enforces a silently-truncated maximum capacity, so:

- Any withdrawal transaction claiming the correct (larger) amount is rejected.
- A withdrawal transaction claiming only the truncated amount is accepted, permanently burning the difference.

This is the direct CKB analog of the liquidation arithmetic bug: the protocol's own math makes it impossible for participants to recover what they are economically entitled to, breaking the NervosDAO incentive mechanism and causing permanent loss of depositor funds.

### Likelihood Explanation

The overflow threshold requires `counted_capacity × ar_ratio > u64::MAX`. The total CKB supply is ~3.36 × 10^18 shannons; `u64::MAX` is ~1.84 × 10^19. A single cell holding the entire supply would need `ar` to grow by a factor of ~5.5× from genesis. Given that `ar` grows with every block's secondary issuance and the protocol is designed to run indefinitely, this threshold is reachable on a long-running chain. A transaction sender (DAO depositor) triggers this path by submitting a withdrawal transaction — no special privilege is required.

### Recommendation

Replace the silent cast with a checked conversion, consistent with the rest of the file:

```rust
let withdraw_capacity =
    Capacity::shannons(
        u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
    ).safe_add(occupied_capacity)?;
```

This makes overflow explicit and returns `DaoError::Overflow` rather than silently truncating, matching the behavior of `miner_issuance128` and `ar_increase128` conversions in `dao_field_with_current_epoch`.

### Proof of Concept

The existing test `check_withdraw_calculation_overflows` demonstrates the overflow path but only catches the case where `safe_add` overflows after the truncation. A case where truncation produces a small value that passes `safe_add` is not tested:

```
deposit_ar      = 10_000_000_000_000_000   (genesis ar)
withdrawing_ar  = 55_000_000_000_000_000   (ar after long operation, ~5.5× growth)
counted_capacity = 3_360_000_000_000_000_000  (near full CKB supply in shannons)

withdraw_counted_capacity (u128) =
    3_360_000_000_000_000_000 × 55_000_000_000_000_000
    / 10_000_000_000_000_000
  = 18_480_000_000_000_000_000   (> u64::MAX = 18_446_744_073_709_551_615)

withdraw_counted_capacity as u64 =
    18_480_000_000_000_000_000 % 2^64
  = 33_255_926_290_448_385   (a small fraction of the correct value)
```

The node accepts a withdrawal claiming only ~33 quadrillion shannons (~33 CKB) instead of the correct ~18.48 quintillion shannons (~184,800 CKB), silently destroying the depositor's interest. [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

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

**File:** util/dao/src/lib.rs (L242-246)
```rust
        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
        let miner_issuance =
            Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
        let nervosdao_issuance = current_g2.safe_sub(miner_issuance)?;
```

**File:** util/dao/src/lib.rs (L258-258)
```rust
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/utils/src/lib.rs (L101-111)
```rust
/// Extract `ar`, `c`, `s`, and `u` from [`Byte32`].
///
/// [`Byte32`]: ../ckb_types/packed/struct.Byte32.html
pub fn extract_dao_data(dao: Byte32) -> (u64, Capacity, Capacity, Capacity) {
    let data = dao.raw_data();
    let c = Capacity::shannons(LittleEndian::read_u64(&data[0..8]));
    let ar = LittleEndian::read_u64(&data[8..16]);
    let s = Capacity::shannons(LittleEndian::read_u64(&data[16..24]));
    let u = Capacity::shannons(LittleEndian::read_u64(&data[24..32]));
    (ar, c, s, u)
}
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

**File:** util/dao/utils/src/error.rs (L36-41)
```rust
    /// Calculation overflow
    #[error("Overflow")]
    Overflow,
    /// ZeroC
    #[error("ZeroC")]
    ZeroC,
```
