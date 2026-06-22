### Title
Silent `u128`→`u64` Truncation in `calculate_maximum_withdraw` Causes Incorrect DAO Withdrawal Capacity Accounting — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes an intermediate `u128` result for the withdrawable capacity but silently truncates it to `u64` via an unchecked `as u64` cast. Every other analogous `u128`→`u64` narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. The inconsistency means that if the product overflows `u64::MAX`, the returned `withdraw_capacity` is silently wrong — a direct analog to the ERC20 fee-on-transfer class where the protocol accounts for a different amount than what actually moves.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the maximum CKBytes a depositor may withdraw from the NervosDAO:

```rust
// util/dao/src/lib.rs  lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
```

`withdraw_counted_capacity` is a `u128`. The cast `as u64` silently wraps modulo 2⁶⁴ if the value exceeds `u64::MAX`. The resulting `withdraw_capacity` is then returned as the authoritative maximum the DAO type script enforces.

Every other `u128`→`u64` narrowing in the same file is guarded:

```rust
// lines 244-245
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);

// line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

The same `calculate_maximum_withdraw` result feeds two downstream paths:

1. **`transaction_maximum_withdraw`** (lines 38-124) — used by `DaoCalculator::transaction_fee` and by `FeeCalculator::transaction_fee` inside `ContextualTransactionVerifier::verify` (verification/src/transaction_verifier.rs lines 265-273). A truncated result changes the computed fee, potentially allowing a transaction that should be rejected to pass, or rejecting a valid one.

2. **`withdrawed_interests`** (lines 312-333) — called inside `dao_field_with_current_epoch` (lines 209-264) which produces the `dao` field committed into every block header. A truncated `maximum_withdraws` corrupts `current_s` (the NervosDAO secondary-issuance accumulator) for every subsequent block, permanently skewing all future DAO interest calculations. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

---

### Impact Explanation

| Scenario | Effect |
|---|---|
| Truncated value < actual value | Depositor's `withdraw_capacity` is understated; the DAO type script enforces a lower ceiling, so the user cannot reclaim their full principal + interest (loss of funds). |
| Truncated value < `input_capacities` | `withdrawed_interests` underflows → `safe_sub` returns `DaoError::Overflow` → the block containing the DAO withdrawal is rejected (consensus-level DoS for that withdrawal). |
| Truncated `maximum_withdraws` propagates into `current_s` | The DAO secondary-issuance accumulator stored in every subsequent block header is permanently wrong, corrupting all future interest calculations chain-wide. |

The `RewardVerifier` enforces `cellbase.outputs_capacity() == block_reward.total` (contextual_block_verifier.rs line 259), and `DaoHeaderVerifier` checks the committed `dao` field. A corrupted `current_s` would cause `DaoHeaderVerifier` to reject otherwise-valid blocks, or accept blocks with wrong DAO state depending on which side of the truncation the error falls. [6](#0-5) 

---

### Likelihood Explanation

For the overflow to trigger:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX  ≈ 1.84 × 10¹⁹
```

- `counted_capacity` ≤ total CKB supply ≈ 3.36 × 10¹⁸ shannons  
- Initial AR = 10¹⁰; the ratio `withdrawing_ar / deposit_ar` must exceed **≈ 5.47×**

Given the current secondary issuance schedule (~1.344 billion CKBytes/year against a ~33.6 billion CKBytes base), the AR grows extremely slowly. Reaching a 5.47× ratio would require centuries of chain operation. **Likelihood is very low under current economics**, but the bug is a latent time-bomb: the inconsistency with every other `u128`→`u64` conversion in the same file is a clear defect regardless of when it becomes reachable.

---

### Recommendation

Replace the silent cast with the same checked pattern used everywhere else in the file:

```rust
// util/dao/src/lib.rs  lines 155-156  — proposed fix
let withdraw_capacity =
    Capacity::shannons(
        u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
    )
    .safe_add(occupied_capacity)?;
```

This makes overflow explicit and consistent with `miner_issuance128` and `ar_increase128` handling in the same function. [7](#0-6) 

---

### Proof of Concept

**Root cause — one silent cast vs. three checked conversions in the same file:**

```rust
// VULNERABLE  (line 156)
Capacity::shannons(withdraw_counted_capacity as u64)

// SAFE pattern used elsewhere in the same file (lines 244-245, 258)
u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?
u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?
```

**Trigger path (transaction sender → consensus):**

1. A transaction sender submits a DAO Phase-2 withdrawal transaction via RPC (`send_transaction`) or P2P relay.
2. `ContextualTransactionVerifier::verify` calls `FeeCalculator::transaction_fee` → `DaoCalculator::transaction_fee` → `transaction_maximum_withdraw` → `calculate_maximum_withdraw`.
3. If `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX`, the `as u64` cast wraps, returning a wrong `withdraw_capacity`.
4. The wrong value propagates into the fee check (`check_tx_fee` in `tx-pool/src/util.rs` line 34) and into `dao_field_with_current_epoch` → `withdrawed_interests` → `current_s`, corrupting the DAO field committed in the block header.
5. `DaoHeaderVerifier` and `RewardVerifier` operate on the corrupted field for all subsequent blocks. [8](#0-7) [9](#0-8) [10](#0-9)

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

**File:** util/dao/src/lib.rs (L126-159)
```rust
    /// Calculate maximum withdraw capacity of a deposited dao output
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
    }
```

**File:** util/dao/src/lib.rs (L209-264)
```rust
    pub fn dao_field_with_current_epoch(
        &self,
        rtxs: impl Iterator<Item = &'a ResolvedTransaction> + Clone,
        parent: &HeaderView,
        current_block_epoch: &EpochExt,
    ) -> Result<Byte32, DaoError> {
        // Freed occupied capacities from consumed inputs
        let freed_occupied_capacities =
            rtxs.clone().try_fold(Capacity::zero(), |capacities, rtx| {
                self.input_occupied_capacities(rtx)
                    .and_then(|c| capacities.safe_add(c))
            })?;
        let added_occupied_capacities = self.added_occupied_capacities(rtxs.clone())?;
        let withdrawed_interests = self.withdrawed_interests(rtxs)?;

        let (parent_ar, parent_c, parent_s, parent_u) = extract_dao_data(parent.dao());

        // g contains both primary issuance and secondary issuance,
        // g2 is the secondary issuance for the block, which consists of
        // issuance for the miner, NervosDAO and treasury.
        // When calculating issuance in NervosDAO, we use the real
        // issuance for each block(which will only be issued on chain
        // after the finalization delay), not the capacities generated
        // in the cellbase of current block.
        let current_block_number = parent.number() + 1;
        let current_g2 = current_block_epoch.secondary_block_issuance(
            current_block_number,
            self.consensus.secondary_epoch_reward(),
        )?;
        let current_g = current_block_epoch
            .block_reward(current_block_number)
            .and_then(|c| c.safe_add(current_g2))?;

        let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
            / u128::from(parent_c.as_u64());
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
        let current_ar = parent_ar
            .checked_add(ar_increase)
            .ok_or(DaoError::Overflow)?;

        Ok(pack_dao_data(current_ar, current_c, current_s, current_u))
    }
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

**File:** verification/src/transaction_verifier.rs (L265-273)
```rust
    fn transaction_fee(&self) -> Result<Capacity, DaoError> {
        // skip tx fee calculation for cellbase
        if self.transaction.is_cellbase() {
            Ok(Capacity::zero())
        } else {
            DaoCalculator::new(self.consensus.as_ref(), &self.data_loader)
                .transaction_fee(&self.transaction)
        }
    }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L237-275)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let cellbase = &self.resolved[0];
        let no_finalization_target =
            (self.parent.number() + 1) <= self.context.consensus.finalization_delay_length();

        let (target_lock, block_reward) = self.context.finalize_block_reward(self.parent)?;
        let output = CellOutput::new_builder()
            .capacity(block_reward.total)
            .lock(target_lock.clone())
            .build();
        let insufficient_reward_to_create_cell = output.is_lack_of_capacity(Capacity::zero())?;

        if no_finalization_target || insufficient_reward_to_create_cell {
            let ret = if cellbase.transaction.outputs().is_empty() {
                Ok(())
            } else {
                Err((CellbaseError::InvalidRewardTarget).into())
            };
            return ret;
        }

        if !insufficient_reward_to_create_cell {
            if cellbase.transaction.outputs_capacity()? != block_reward.total {
                return Err((CellbaseError::InvalidRewardAmount).into());
            }
            if cellbase
                .transaction
                .outputs()
                .get(0)
                .expect("cellbase should have output")
                .lock()
                != target_lock
            {
                return Err((CellbaseError::InvalidRewardTarget).into());
            }
        }

        Ok(())
    }
```

**File:** tx-pool/src/util.rs (L28-54)
```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
    Ok(fee)
}
```
