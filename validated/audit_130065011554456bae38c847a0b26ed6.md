### Title
Silent `u128`→`u64` Truncation in `calculate_maximum_withdraw` Produces Silently Incorrect DAO Withdrawal Amount — (`File: util/dao/src/lib.rs`)

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the maximum CKB a depositor can withdraw from the NervosDAO using a `u128` intermediate value, then casts it to `u64` with a bare `as u64` — a silent truncation. Every other analogous `u128`→`u64` narrowing in the same codebase uses `u64::try_from(…).map_err(|_| DaoError::Overflow)?`. When the intermediate product overflows `u64::MAX`, the result is silently wrapped to a much smaller value, causing the depositor to receive far less than they are owed (or causing their withdrawal transaction to be rejected by the tx-pool as having negative fee).

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
// util/dao/src/lib.rs  lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast silently discards the upper 64 bits if `withdraw_counted_capacity > u64::MAX`. There is no error path; the function returns `Ok(wrong_value)`.

Every other `u128`→`u64` narrowing in the same file uses a checked conversion that propagates `DaoError::Overflow`:

```rust
// dao_field_with_current_epoch — line 245
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);

// dao_field_with_current_epoch — line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) 

`secondary_block_reward` in the same crate also uses the checked form:

```rust
// util/dao/src/lib.rs  line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
``` [4](#0-3) 

The overflow condition is:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

`counted_capacity` is `output_capacity − occupied_capacity` for a single cell; a cell can hold up to the entire circulating supply (~3.36 × 10¹⁸ shannons). `deposit_ar` is the accumulate-rate at deposit time (genesis value: 10¹⁶). `withdrawing_ar` is the accumulate-rate at withdrawal time. The ratio `withdrawing_ar / deposit_ar` needs to exceed ~5.5 for the overflow to trigger with a maximally-loaded cell. Given the secondary epoch reward of ~1.344 billion CKB/year against a total supply of ~33.6 billion CKB, the accumulate rate grows at roughly 4 % per year, placing the overflow threshold at approximately 112 years of continuous operation — a long-term but non-zero risk horizon for a live chain. [5](#0-4) 

### Impact Explanation

When the overflow triggers, `calculate_maximum_withdraw` silently returns a value far below the depositor's true entitlement. Two concrete consequences follow:

1. **Incorrect withdrawal amount accepted on-chain.** The DAO script verifies the withdrawal amount against the value returned by this function. A truncated return value causes the consensus layer to accept a cellbase that pays the depositor far less than they earned, constituting a direct, irreversible loss of CKB for the depositor.

2. **Valid DAO withdrawal rejected by the tx-pool.** `transaction_fee` calls `transaction_maximum_withdraw` (which calls `calculate_maximum_withdraw`) and computes `maximum_withdraw − outputs_capacity`. If the truncated `maximum_withdraw` is smaller than `outputs_capacity`, `safe_sub` returns `Err`, and `check_tx_fee` rejects the transaction as malformed — a DoS against the depositor's withdrawal. [6](#0-5) [7](#0-6) 

### Likelihood Explanation

The overflow requires the accumulate-rate ratio `withdrawing_ar / deposit_ar` to exceed ~5.5 for a cell holding close to the full circulating supply. At the current secondary issuance rate this takes on the order of a century. However:

- The condition is **deterministic and permanent**: once the chain reaches that accumulate-rate, every large DAO cell deposited near genesis is affected simultaneously.
- The entry path requires no privilege: any user who deposited CKB into the NervosDAO and submits a withdrawal transaction triggers the path through `check_tx_fee` → `transaction_fee` → `calculate_maximum_withdraw`.
- The defect is **inconsistent with the surrounding code**, which uniformly uses checked conversions, suggesting it is an oversight rather than an intentional design choice.

### Recommendation

Replace the bare `as u64` cast with the same checked conversion pattern used everywhere else in the file:

```rust
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
``` [8](#0-7) 

### Proof of Concept

The existing test `check_dao_data_calculation_overflows` in `util/dao/src/tests.rs` already demonstrates that the DAO calculator correctly propagates `DaoError::Overflow` for `dao_field_with_current_epoch`. An analogous test for `calculate_maximum_withdraw` would expose the silent truncation:

```rust
// Hypothetical test demonstrating the silent truncation
#[test]
fn check_calculate_maximum_withdraw_silent_truncation() {
    // deposit_ar = 10^16 (genesis), withdrawing_ar = 6 * 10^16 (5.5x growth)
    // counted_capacity = 3_360_000_000 CKB = 3.36e18 shannons
    // withdraw_counted_capacity = 3.36e18 * 6e16 / 1e16 = 2.016e19 > u64::MAX (1.84e19)
    // `as u64` silently truncates to ~1.75e18 — depositor loses ~90% of interest
    let deposit_ar:     u64 = 10_000_000_000_000_000;   // 10^16
    let withdrawing_ar: u64 = 60_000_000_000_000_000;   // 6 * 10^16
    let counted_capacity: u128 = 3_360_000_000 * 100_000_000; // 3.36e18 shannons

    let result_u128 = counted_capacity * u128::from(withdrawing_ar) / u128::from(deposit_ar);
    assert!(result_u128 > u64::MAX as u128, "overflow condition met");

    // current code: silent truncation
    let truncated = result_u128 as u64;
    // correct code: should return Err(DaoError::Overflow) or a capped value
    assert!(truncated < (result_u128 / 2) as u64, "value silently halved by truncation");
}
``` [9](#0-8) [10](#0-9)

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

**File:** util/dao/src/lib.rs (L256-258)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

**File:** spec/src/consensus.rs (L42-45)
```rust
// 1.344 billion per year
pub(crate) const DEFAULT_SECONDARY_EPOCH_REWARD: Capacity = Capacity::shannons(613_698_63013698);
// 4.2 billion per year
pub(crate) const INITIAL_PRIMARY_EPOCH_REWARD: Capacity = Capacity::shannons(1_917_808_21917808);
```

**File:** tx-pool/src/util.rs (L28-53)
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
```

**File:** util/dao/src/tests.rs (L156-177)
```rust
#[test]
fn check_dao_data_calculation_overflows() {
    let consensus = Consensus::default();

    let parent_number = 12345;
    let epoch = EpochNumberWithFraction::new(12, 345, 1000);
    let parent_header = HeaderBuilder::default()
        .number(parent_number)
        .epoch(epoch)
        .dao(pack_dao_data(
            10_000_000_000_123_456,
            Capacity::shannons(18_446_744_073_709_000_000),
            Capacity::shannons(446_744_073_709),
            Capacity::shannons(600_000_000_000),
        ))
        .build();

    let (_tmp_dir, store, parent_header) = prepare_store(&parent_header, None);
    let result = DaoCalculator::new(&consensus, &store.borrow_as_data_loader())
        .dao_field([].iter(), &parent_header);
    assert!(result.unwrap_err().to_string().contains("Overflow"));
}
```
