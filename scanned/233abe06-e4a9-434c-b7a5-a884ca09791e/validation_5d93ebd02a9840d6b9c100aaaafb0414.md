### Title
Silent `u128`→`u64` Truncation in DAO Withdrawal Capacity Scaling Produces Incorrect Withdrawal Amounts — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` scales a depositor's capacity by the ratio of the withdrawal-block AR to the deposit-block AR. The intermediate result is held in a `u128`, but is then cast to `u64` with a bare `as u64` (line 156), which **silently wraps/truncates** if the value exceeds `u64::MAX`. Every other analogous calculation in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`, which returns a proper error. The inconsistency means that once the AR ratio has grown enough, a large DAO deposit will produce a silently wrong (drastically smaller) withdrawal amount rather than a detectable error.

---

### Finding Description

In `calculate_maximum_withdraw` (`util/dao/src/lib.rs`):

```rust
// lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
```

The AR (accumulate rate) stored in every block's DAO field is a monotonically increasing "floating" scalar — exactly analogous to a rebase multiplier. It starts at `10^16` and grows each block by `AR_old * g2 / C`. The withdrawal formula is:

```
withdraw_counted_capacity = counted_capacity * withdrawing_AR / deposit_AR
```

Because `counted_capacity` is a `u64` and `withdrawing_AR / deposit_AR > 1`, the product can exceed `u64::MAX`. The `u128` intermediate prevents the multiplication from overflowing, but the final `as u64` cast **wraps modulo 2^64**, silently producing a value that is billions of shannons smaller than the correct answer.

Compare with the three other identical-pattern calculations in the same file, all of which use the safe form:

- Line 244–245: `u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?`
- Line 258: `u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?`
- Line 204 (`secondary_block_reward`): `u64::try_from(reward128).map_err(|_| DaoError::Overflow)?`

The `calculate_maximum_withdraw` path is the only one that uses the unsafe `as u64` cast. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

---

### Impact Explanation

`calculate_maximum_withdraw` is called in two reachable paths:

1. **`transaction_maximum_withdraw` → `transaction_fee`** — used by the node to verify that a DAO withdrawal transaction's output capacity does not exceed the entitled maximum. If the truncated value is smaller than the actual output capacity, `safe_sub` returns a `CapacityError::Overflow`, and the node **rejects the withdrawal transaction entirely**, even though it is economically valid. The depositor cannot recover their funds through the normal withdrawal path.

2. **`calculate_dao_maximum_withdraw` RPC** — used by wallets and users to learn how much they can withdraw. If the RPC returns the truncated (wrong) value and the user constructs a transaction claiming only that amount, the transaction is accepted on-chain but the depositor **permanently loses the difference** between the correct and truncated amounts. [5](#0-4) [6](#0-5) 

---

### Likelihood Explanation

The overflow condition is:

```
counted_capacity × withdrawing_AR / deposit_AR  >  u64::MAX  (≈ 1.844 × 10¹⁹)
```

The total CKB supply is approximately 33.6 billion CKB = 3.36 × 10¹⁸ shannons. The AR starts at `10^16` and grows at roughly 4 % per year (secondary issuance ≈ 1.344 billion CKB/year over a ~33.6 billion CKB base). For a deposit equal to the entire circulating supply, the overflow threshold is reached when `AR ≈ 5.49 × 10^16`, i.e., after roughly **42 years** of continuous growth. For smaller deposits the threshold is proportionally later.

While the timeline is long, the vulnerability is structurally present from genesis, the fix is trivial, and the consequence when triggered is either permanent fund loss or permanent withdrawal denial — both high-severity outcomes for the affected depositor. [7](#0-6) 

---

### Recommendation

Replace the silent cast with the same checked conversion used everywhere else in the file:

```rust
// Before (line 155-156):
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

// After:
let withdraw_capacity = Capacity::shannons(
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
).safe_add(occupied_capacity)?;
```

This makes the overflow detectable and consistent with the rest of the DAO accounting code. [8](#0-7) 

---

### Proof of Concept

**Numeric example (boundary condition):**

| Parameter | Value |
|---|---|
| `counted_capacity` | `3.36 × 10¹⁸` shannons (≈ entire CKB supply) |
| `deposit_AR` | `10^16` (genesis AR) |
| `withdrawing_AR` | `5.5 × 10^16` (AR after ~42 years) |
| `withdraw_counted_capacity` (u128) | `3.36 × 10¹⁸ × 5.5 × 10^16 / 10^16 = 1.848 × 10¹⁹` |
| `u64::MAX` | `1.844 × 10¹⁹` |
| `withdraw_counted_capacity as u64` | `1.848 × 10¹⁹ − 2^64 ≈ 3.6 × 10¹⁷` (wrong — ~10× too small) |

**Entry path:**
1. A large DAO depositor calls `calculate_dao_maximum_withdraw` RPC.
2. The RPC calls `DaoCalculator::calculate_maximum_withdraw` with the deposit and withdrawal block headers.
3. The `as u64` truncation returns `≈ 3.6 × 10¹⁷` shannons instead of `≈ 1.848 × 10¹⁹` shannons.
4. The depositor constructs a withdrawal transaction claiming `3.6 × 10¹⁷` shannons.
5. The transaction is accepted on-chain; the depositor permanently loses `≈ 1.488 × 10¹⁹` shannons.

Alternatively, if the depositor independently computes the correct amount (`1.848 × 10¹⁹`), the node's `transaction_fee` calculation returns `maximum_withdraw (3.6 × 10¹⁷) − outputs_capacity (1.848 × 10¹⁹)` → `safe_sub` underflow → `DaoError::Overflow` → transaction rejected. [9](#0-8) [10](#0-9)

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

**File:** util/dao/src/lib.rs (L38-124)
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

**File:** rpc/src/module/experiment.rs (L235-298)
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
            }
            DaoWithdrawingCalculationKind::WithdrawingOutPoint(withdrawing_out_point) => {
                let (_tx, deposit_header_hash) = snapshot
                    .get_transaction(&out_point.tx_hash())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;

                let withdrawing_out_point: packed::OutPoint = withdrawing_out_point.into();
                let (withdrawing_tx, withdrawing_header_hash) = snapshot
                    .get_transaction(&withdrawing_out_point.tx_hash())
                    .ok_or_else(|| RPCError::invalid_params("invalid withdrawing_out_point"))?;

                let output = withdrawing_tx
                    .outputs()
                    .get(withdrawing_out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid withdrawing_out_point"))?;
                let output_data = withdrawing_tx
                    .outputs_data()
                    .get(withdrawing_out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid withdrawing_out_point"))?;

                match calculator.calculate_maximum_withdraw(
                    &output,
                    core::Capacity::bytes(output_data.len()).expect("should not overflow"),
                    &deposit_header_hash,
                    &withdrawing_header_hash,
                ) {
                    Ok(capacity) => Ok(capacity.into()),
                    Err(err) => Err(RPCError::custom_with_error(RPCError::DaoError, err)),
                }
            }
        }
```
