### Title
Silent Truncating Cast in `calculate_maximum_withdraw` Yields Wrong DAO Withdrawal Capacity — (File: `util/dao/src/lib.rs`)

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes an intermediate `u128` result and then silently truncates it to `u64` via an `as u64` cast. Every other analogous u128→u64 narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. The inconsistency means that when the intermediate product overflows `u64::MAX`, the result is silently wrong rather than returning an error, causing downstream fee-calculation logic to produce an incorrect value and reject a valid DAO withdrawal transaction.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the withdrawable capacity as:

```rust
// util/dao/src/lib.rs  lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast silently discards the upper 64 bits when `withdraw_counted_capacity > u64::MAX`, producing a wrong (smaller) capacity value with no error signal.

Every other u128→u64 narrowing in the same file is guarded:

```rust
// secondary_block_reward – line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;

// dao_field_with_current_epoch – line 245
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);

// dao_field_with_current_epoch – line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

`calculate_maximum_withdraw` is the sole exception.

---

### Impact Explanation

`calculate_maximum_withdraw` is called on two paths:

1. **Transaction verification (consensus path):** `FeeCalculator::transaction_fee` → `transaction_maximum_withdraw` → `calculate_maximum_withdraw`. If the truncated `withdraw_capacity` is smaller than the transaction's `outputs_capacity`, the subtraction in `transaction_fee` underflows and the transaction is rejected with a `DaoError` even though it is protocol-valid. This is a **denial-of-service on DAO withdrawal transactions** for affected depositors. [5](#0-4) 

2. **RPC path:** `calculate_dao_maximum_withdraw` returns a silently wrong (too-small) capacity to the caller, misleading wallets and tooling about the actual withdrawable amount. [6](#0-5) 

---

### Likelihood Explanation

The overflow condition is:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

`ar` (accumulation rate) starts at `10_000_000_000_000_000` (10^16) and grows at roughly 4 % per year from secondary issuance. [7](#0-6) 

For the ratio `withdrawing_ar / deposit_ar` to exceed ~4.3 (the threshold at maximum realistic `counted_capacity` of ~4.2 × 10^18 shannons given the bounded total CKB supply), approximately 37 years of chain operation are required. The likelihood is therefore **low in the near term** but the code is demonstrably inconsistent with every other u128→u64 narrowing in the same file, and the silent truncation is a latent correctness defect that will eventually become reachable.

---

### Recommendation

Replace the silent `as u64` cast with the same checked pattern used everywhere else in the file:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(
        u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
    )
    .safe_add(occupied_capacity)?;
``` [1](#0-0) 

---

### Proof of Concept

```rust
// Demonstrates the silent truncation in calculate_maximum_withdraw.
// Set deposit_ar = 1, withdrawing_ar = 2, counted_capacity = u64::MAX / 2 + 1.
// Correct result: (u64::MAX/2 + 1) * 2 / 1 = u64::MAX + 1  → should error.
// Actual result:  (u64::MAX + 1) as u64 = 0                 → silently wrong.

let counted_capacity: u64 = u64::MAX / 2 + 1;
let deposit_ar:       u64 = 1;
let withdrawing_ar:   u64 = 2;

let withdraw_counted_capacity: u128 =
    u128::from(counted_capacity) * u128::from(withdrawing_ar) / u128::from(deposit_ar);
// withdraw_counted_capacity == u64::MAX + 1  (fits in u128, overflows u64)

let wrong_result = withdraw_counted_capacity as u64;  // == 0  (silent truncation)
assert_eq!(wrong_result, 0);  // passes — but the correct answer is an overflow error
```

The attacker-controlled entry point is a DAO withdrawal transaction whose depositing cell holds a large capacity and whose deposit block has a sufficiently smaller `ar` than the withdrawing block. No privileged access is required; any transaction sender can trigger this path through the normal DAO withdrawal flow.

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

**File:** util/dao/src/lib.rs (L258-258)
```rust
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

**File:** rpc/src/module/experiment.rs (L107-131)
```rust
    /// a withdrawing block hash.
    ///
    /// ## Params
    ///
    /// * `out_point` - Reference to the DAO cell, the depositing transaction's output.
    /// * `kind` - Two kinds of dao withdrawal amount calculation option.
    ///
    /// option 1, the assumed reference block hash for withdrawing phase 1 transaction, this block must be in the
    /// [canonical chain](trait.ChainRpc.html#canonical-chain), the calculation of occupied capacity will be based on the depositing transaction's output, assuming the output of phase 1 transaction is the same as the depositing transaction's output.
    ///
    /// option 2, the out point of the withdrawing phase 1 transaction, the calculation of occupied capacity will be based on corresponding phase 1 transaction's output.
    ///
    /// ## Returns
    ///
    /// The RPC returns the final capacity when the cell `out_point` is withdrawn using the block hash or withdrawing phase 1 transaction out point as the reference.
    ///
    /// In CKB, scripts cannot get the information about in which block the transaction is
    /// committed. A workaround is letting the transaction reference a block hash so the script
    /// knows that the transaction is committed at least after the reference block.
    ///
    /// ## Errors
    ///
    /// * [`DaoError (-5)`](../enum.RPCError.html#variant.DaoError) - The given out point is not a valid cell for DAO computation.
    /// * [`CKBInternalError (-1)`](../enum.RPCError.html#variant.CKBInternalError) - Mathematics overflow.
    ///
```

**File:** util/dao/utils/src/lib.rs (L1-10)
```rust
//! This crate provides several util functions to operate the dao field and NervosDAO related errors.

mod error;

use byteorder::{ByteOrder, LittleEndian};
use ckb_types::{
    H160,
    core::{Capacity, Ratio, TransactionView, capacity_bytes},
    packed::{Byte32, OutPoint},
    prelude::*,
```
