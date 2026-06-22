### Title
Silent Truncating Cast in DAO Withdrawal Capacity Calculation Produces Incorrect Withdrawal Amount - (File: util/dao/src/lib.rs)

### Summary
In `DaoCalculator::calculate_maximum_withdraw`, the intermediate u128 result `withdraw_counted_capacity` is cast back to u64 using the silent `as u64` operator instead of the checked `u64::try_from(...).map_err(|_| DaoError::Overflow)?` pattern used consistently everywhere else in the same file. If the u128 value exceeds `u64::MAX`, the cast silently truncates it, producing a drastically underestimated withdrawal capacity. This causes the downstream fee calculation (`maximum_withdraw.safe_sub(outputs_capacity)`) to fail with an underflow error, permanently blocking any DAO depositor whose deposit is large enough and whose deposit was made early enough in the chain's life from ever withdrawing.

### Finding Description

In `util/dao/src/lib.rs`, the function `calculate_maximum_withdraw` computes the interest-bearing withdrawal amount using u128 arithmetic to avoid intermediate overflow, but then casts the result back to u64 with a bare `as u64`:

```rust
// util/dao/src/lib.rs lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

The same file uses the safe, checked conversion pattern in every analogous calculation:

```rust
// util/dao/src/lib.rs line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
// util/dao/src/lib.rs line 245
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)
// util/dao/src/lib.rs line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) [4](#0-3) 

The `as u64` cast is a **silent truncating cast** in Rust: if `withdraw_counted_capacity > u64::MAX`, the high bits are discarded with no error, no panic, and no indication to the caller.

The overflow condition is:

```
withdraw_counted_capacity = counted_capacity × (withdrawing_ar / deposit_ar) > u64::MAX
```

- `counted_capacity` is at most the total CKB supply in shannons (~3.36 × 10¹⁸)
- `withdrawing_ar / deposit_ar` is the NervosDAO accumulation rate ratio (interest multiplier)
- The initial `ar` is `10_000_000_000_000_000` (10¹⁶); it grows with each block's secondary issuance
- When the ratio exceeds ~5.5×, a maximum-sized deposit triggers the truncation

The call chain that reaches this code during normal transaction processing is:

`ContextualTransactionVerifier::verify` → `FeeCalculator::transaction_fee` → `DaoCalculator::transaction_fee` → `transaction_maximum_withdraw` → `calculate_maximum_withdraw` [5](#0-4) [6](#0-5) [7](#0-6) 

### Impact Explanation

When `withdraw_counted_capacity` is silently truncated, `withdraw_capacity` (the computed maximum withdrawal) becomes a small, incorrect value. In `transaction_fee`, the node then computes:

```rust
maximum_withdraw.safe_sub(outputs_capacity)
``` [6](#0-5) 

If the truncated `maximum_withdraw` is less than `outputs_capacity` (the legitimate withdrawal amount the user is claiming), `safe_sub` returns `CapacityError::Overflow`, which propagates as a `DaoError::Overflow` and causes the transaction to be rejected. The depositor cannot withdraw their funds. Because the error is deterministic and tied to the on-chain state (the ar values), the DoS is permanent for that depositor — no retry will succeed.

Additionally, even if the truncated value happens to be larger than `outputs_capacity`, the fee is computed incorrectly (too large), meaning the node accepts the transaction but the depositor effectively donates excess CKB to the miner.

### Likelihood Explanation

The overflow requires `withdrawing_ar / deposit_ar ≳ 5.5`. The NervosDAO secondary issuance rate is approximately 1.344 billion CKB/year against a base of ~33.6 billion CKB, yielding roughly 4% annual growth in `ar`. At compound growth, the ratio reaches 5.5× after approximately 42 years of chain operation (around year 2061 for mainnet). The vulnerability therefore does not pose an immediate threat but is a latent time-bomb: the inconsistency with the rest of the file's checked-conversion pattern is a clear defect, and the impact when triggered is a permanent, irreversible DoS on DAO withdrawals for large, early depositors.

### Recommendation

Replace the silent cast with the same checked-conversion pattern used everywhere else in the file:

```rust
// Before (unsafe):
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

// After (safe, consistent with the rest of the file):
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
``` [8](#0-7) 

### Proof of Concept

The inconsistency is directly visible by comparing line 156 with lines 204, 245, and 258 in the same file. The `DaoError::Overflow` variant already exists and is already returned by the other three analogous calculations in `dao_field_with_current_epoch` and `secondary_block_reward`; the `calculate_maximum_withdraw` function simply omits the check. [9](#0-8) [10](#0-9)

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

**File:** util/dao/src/lib.rs (L38-123)
```rust
    fn transaction_maximum_withdraw(
        &self,
        rtx: &ResolvedTransaction,
    ) -> Result<Capacity, DaoError> {
        let header_deps: HashSet<Byte32> = rtx.transaction.header_deps_iter().collect();
        rtx.resolved_inputs.iter().enumerate().try_fold(
            Capacity::zero(),
            |capacities, (i, cell_meta)| {
                let capacity: Result<Capacity, DaoError> = {
                    let output = &cell_meta.cell_output;
                    let is_dao_type_script = |type_script: Script| {
                        Into::<u8>::into(type_script.hash_type())
                            == Into::<u8>::into(ScriptHashType::Type)
                            && type_script.code_hash() == self.consensus.dao_type_hash()
                    };
                    let is_dao_output = output
                        .type_()
                        .to_opt()
                        .map(is_dao_type_script)
                        .unwrap_or(false);
                    if is_dao_output {
                        // A withdrawing DAO cell has 8 bytes of cell data storing the
                        // block number of the original deposit.
                        let deposited_block_number =
                            match self.data_loader.load_cell_data(cell_meta) {
                                Some(data) if data.len() == 8 => LittleEndian::read_u64(&data),
                                _ => 0,
                            };
                        if deposited_block_number > 0 {
                            let withdrawing_header_hash = cell_meta
                                .transaction_info
                                .as_ref()
                                .map(|info| &info.block_hash)
                                .filter(|hash| header_deps.contains(hash))
                                .ok_or(DaoError::InvalidOutPoint)?;
                            let deposit_header_hash = rtx
                                .transaction
                                .witnesses()
                                .get(i)
                                .ok_or(DaoError::InvalidOutPoint)
                                .and_then(|witness_data| {
                                    // dao contract stores header deps index as u64 in the input_type field of WitnessArgs
                                    let witness =
                                        WitnessArgs::from_slice(&Into::<Bytes>::into(witness_data))
                                            .map_err(|_| DaoError::InvalidDaoFormat)?;
                                    let header_deps_index_data: Option<Bytes> =
                                        witness.input_type().to_opt().map(|witness| witness.into());
                                    if header_deps_index_data.is_none()
                                        || header_deps_index_data.clone().map(|data| data.len())
                                            != Some(8)
                                    {
                                        return Err(DaoError::InvalidDaoFormat);
                                    }
                                    Ok(LittleEndian::read_u64(&header_deps_index_data.unwrap()))
                                })
                                .and_then(|header_dep_index| {
                                    rtx.transaction
                                        .header_deps()
                                        .get(header_dep_index as usize)
                                        .and_then(|hash| header_deps.get(&hash))
                                        .ok_or(DaoError::InvalidOutPoint)
                                })?;

                            let deposit_header = self
                                .data_loader
                                .get_header(deposit_header_hash)
                                .ok_or(DaoError::InvalidHeader)?;
                            if deposit_header.number() != deposited_block_number {
                                return Err(DaoError::InvalidOutPoint);
                            }
                            self.calculate_maximum_withdraw(
                                output,
                                Capacity::bytes(cell_meta.data_bytes as usize)?,
                                deposit_header_hash,
                                withdrawing_header_hash,
                            )
                        } else {
                            Ok(output.capacity().into())
                        }
                    } else {
                        Ok(output.capacity().into())
                    }
                };
                capacity.and_then(|c| c.safe_add(capacities).map_err(Into::into))
            },
        )
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

**File:** util/dao/src/lib.rs (L256-261)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
        let current_ar = parent_ar
            .checked_add(ar_increase)
            .ok_or(DaoError::Overflow)?;
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

**File:** util/dao/utils/src/error.rs (L36-41)
```rust
    /// Calculation overflow
    #[error("Overflow")]
    Overflow,
    /// ZeroC
    #[error("ZeroC")]
    ZeroC,
```
