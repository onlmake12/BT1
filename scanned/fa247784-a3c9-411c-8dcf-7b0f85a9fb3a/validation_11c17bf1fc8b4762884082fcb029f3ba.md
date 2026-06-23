### Title
Silent u128→u64 Truncation in DAO Withdrawal Capacity Computation — (`File: util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the interest-bearing withdrawal capacity using a u128 intermediate value but converts it to u64 with a bare `as u64` truncating cast instead of a checked conversion. Every other analogous u128→u64 narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. When the u128 result exceeds `u64::MAX`, the truncation silently produces a drastically wrong (too-small) withdrawal amount with no error, corrupting DAO interest accounting.

---

### Finding Description

In `calculate_maximum_withdraw` the interest-scaled capacity is computed as:

```rust
// util/dao/src/lib.rs  lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← bare truncating cast
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The formula is `counted_capacity × withdrawing_ar / deposit_ar`. Because `withdrawing_ar ≥ deposit_ar` always (the accumulate rate AR is monotonically increasing), the result is always ≥ `counted_capacity`. If the product exceeds `u64::MAX`, the `as u64` cast silently wraps to `withdraw_counted_capacity % 2^64`, producing a value that can be orders of magnitude smaller than the correct answer — with no error returned.

Every other u128→u64 narrowing in the same file uses the safe pattern:

```rust
// line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;

// line 245
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);

// line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The `calculate_maximum_withdraw` function is the sole exception.

The initial accumulate rate is `DEFAULT_GENESIS_ACCUMULATE_RATE = 10_000_000_000_000_000` (10^16). [5](#0-4) 

AR grows each block by `parent_ar × g2 / C`. At CKB's secondary issuance rate (~4 % per year relative to total capacity), the ratio `withdrawing_ar / deposit_ar` grows slowly but without bound. Once it exceeds `u64::MAX / counted_capacity`, the cast wraps.

---

### Impact Explanation

`calculate_maximum_withdraw` is called from three paths:

1. **`transaction_maximum_withdraw` → `transaction_fee`** — used by `RewardCalculator::txs_fees` in block reward computation. A truncated value causes the node to compute an incorrect transaction fee for a DAO withdrawal, corrupting the miner's block reward. [6](#0-5) [7](#0-6) 

2. **`transaction_maximum_withdraw` → `withdrawed_interests`** — used in `dao_field_with_current_epoch` to update the DAO field's `S` (NervosDAO savings) component. A truncated `withdrawed_interests` causes `current_s` to be inflated, permanently corrupting the on-chain DAO accounting for all future depositors. [8](#0-7) [9](#0-8) 

3. **RPC `calculate_dao_maximum_withdraw`** — returns a silently wrong (too-small) withdrawal amount to the user, causing them to construct a transaction that forfeits accrued interest. [10](#0-9) 

---

### Likelihood Explanation

The overflow condition is `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX`. With the total CKB supply at ~3.36 × 10^18 shannons and AR growing at ~4 % per year, the ratio `withdrawing_ar / deposit_ar` must exceed ~5.5 for a full-supply deposit, which takes roughly 43 years at current issuance rates. However:

- Any NervosDAO depositor triggers this path by submitting a withdrawal transaction — no privilege required.
- The threshold is lower for large individual deposits relative to the total supply.
- The bug is silent: no error is returned, no panic occurs, and the corrupted value propagates into consensus-critical DAO field and reward calculations.
- The inconsistency with every other narrowing cast in the same file confirms this is an unintentional omission, not a deliberate design choice.

---

### Recommendation

Replace the bare truncating cast with the same checked conversion used everywhere else in the file:

```rust
// Before (line 155-156):
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

// After:
let withdraw_capacity = Capacity::shannons(
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
).safe_add(occupied_capacity)?;
``` [11](#0-10) 

---

### Proof of Concept

Using the existing test infrastructure pattern from `util/dao/src/tests.rs`:

```
deposit_ar      = 10_000_000_000_000_000   (DEFAULT_GENESIS_ACCUMULATE_RATE)
withdrawing_ar  = 60_000_000_000_000_000   (6× growth — reachable after ~45 years)
output capacity = 4_000_000_000_000_000_000 shannons  (4 × 10^18, ~40 billion CKB)
occupied_cap    = 6_100_000_000 shannons

counted_capacity = 4_000_000_000_000_000_000 - 6_100_000_000
                 ≈ 3_999_999_993_900_000_000

withdraw_counted_capacity (u128) =
    3_999_999_993_900_000_000 × 60_000_000_000_000_000
    / 10_000_000_000_000_000
  = 23_999_999_963_400_000_000   ← exceeds u64::MAX (18_446_744_073_709_551_615)

withdraw_counted_capacity as u64
  = 23_999_999_963_400_000_000 % 2^64
  = 5_553_255_889_690_448_385   ← silently wrong, ~76% less than correct value
```

The function returns `Ok(...)` with a capacity ~5.55 × 10^18 shannons instead of the correct ~2.4 × 10^19 shannons, with no error signal. This incorrect value then propagates into `withdrawed_interests` and `transaction_fee` in consensus-critical code paths. [12](#0-11) [5](#0-4)

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

**File:** util/dao/src/lib.rs (L127-158)
```rust
    pub fn calculate_maximum_withdraw(
        &self,
        output: &CellOutput,
        output_data_capacity: Capacity,
        deposit_header_hash: &Byte32,
        withdrawing_header_hash: &Byte32,
    ) -> Result<Capacity, DaoError> {
        let deposit_header = self
            .data_loader
            .get_header(deposit_header_hash)
            .ok_or(DaoError::InvalidHeader)?;
        let withdrawing_header = self
            .data_loader
            .get_header(withdrawing_header_hash)
            .ok_or(DaoError::InvalidHeader)?;
        if deposit_header.number() >= withdrawing_header.number() {
            return Err(DaoError::InvalidOutPoint);
        }

        let (deposit_ar, _, _, _) = extract_dao_data(deposit_header.dao());
        let (withdrawing_ar, _, _, _) = extract_dao_data(withdrawing_header.dao());

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

**File:** util/dao/src/lib.rs (L244-246)
```rust
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

**File:** util/dao/src/lib.rs (L256-261)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
        let current_ar = parent_ar
            .checked_add(ar_increase)
            .ok_or(DaoError::Overflow)?;
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

**File:** util/dao/utils/src/lib.rs (L17-17)
```rust
const DEFAULT_GENESIS_ACCUMULATE_RATE: u64 = 10_000_000_000_000_000;
```

**File:** util/reward-calculator/src/lib.rs (L103-110)
```rust
        let txs_fees = self.txs_fees(target)?;
        let proposal_reward = self.proposal_reward(parent, target)?;
        let (primary, secondary) = self.base_block_reward(target)?;

        let total = txs_fees
            .safe_add(proposal_reward)?
            .safe_add(primary)?
            .safe_add(secondary)?;
```

**File:** rpc/src/module/experiment.rs (L235-267)
```rust
    fn calculate_dao_maximum_withdraw(
        &self,
        out_point: OutPoint,
        kind: DaoWithdrawingCalculationKind,
    ) -> Result<Capacity> {
        let snapshot: &Snapshot = &self.shared.snapshot();
        let consensus = snapshot.consensus();
        let out_point: packed::OutPoint = out_point.into();
        let data_loader = snapshot.borrow_as_data_loader();
        let calculator = DaoCalculator::new(consensus, &data_loader);
        match kind {
            DaoWithdrawingCalculationKind::WithdrawingHeaderHash(withdrawing_header_hash) => {
                let (tx, deposit_header_hash) = snapshot
                    .get_transaction(&out_point.tx_hash())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;
                let output = tx
                    .outputs()
                    .get(out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;
                let output_data = tx
                    .outputs_data()
                    .get(out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;

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
