### Title
Silent `u128`→`u64` Truncation in `calculate_maximum_withdraw` Silently Corrupts DAO Withdrawal Capacity, Enabling Denial-of-Service on DAO Withdrawals — (`File: util/dao/src/lib.rs`)

---

### Summary

In `DaoCalculator::calculate_maximum_withdraw`, the intermediate `u128` product `withdraw_counted_capacity` is cast to `u64` using a bare `as u64` (a silent, wrapping truncation) instead of a checked conversion. Every other analogous `u128`→`u64` narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. When the product overflows `u64`, the computed withdrawal capacity silently wraps to a value far below the deposited principal, causing the `withdrawed_interests` sub-step to underflow and reject any block that contains the affected DAO withdrawal transaction.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the interest-bearing withdrawal amount as:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast silently wraps when `withdraw_counted_capacity > u64::MAX`. Compare with the `ar_increase` calculation in the same file, which uses a checked conversion:

```rust
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) 

The same inconsistency exists in `secondary_block_reward`, which also uses `u64::try_from`: [3](#0-2) 

When `withdraw_counted_capacity` wraps, the returned `withdraw_capacity` can be far below the deposited `input_capacity`. This propagates into `withdrawed_interests`:

```rust
maximum_withdraws
    .safe_sub(input_capacities)   // underflows → DaoError::Overflow
    .map_err(Into::into)
``` [4](#0-3) 

That error propagates through `dao_field_with_current_epoch`:

```rust
let current_s = parent_s
    .safe_add(nervosdao_issuance)
    .and_then(|s| s.safe_sub(withdrawed_interests))?;
``` [5](#0-4) 

…and ultimately causes `DaoHeaderVerifier::verify` to reject the block: [6](#0-5) 

The same truncated value is used by `transaction_fee` to compute the fee for a DAO withdrawal transaction:

```rust
pub fn transaction_fee(&self, rtx: &ResolvedTransaction) -> Result<Capacity, DaoError> {
    let maximum_withdraw = self.transaction_maximum_withdraw(rtx)?;
    rtx.transaction
        .outputs_capacity()
        .and_then(|y| maximum_withdraw.safe_sub(y))  // underflows if truncated < outputs
        .map_err(Into::into)
}
``` [7](#0-6) 

This means a DAO withdrawal transaction whose true maximum withdrawal exceeds `u64::MAX` shannons will be permanently rejected at the tx-pool admission stage as well.

---

### Impact Explanation

**Two distinct failure modes arise from the same root cause:**

1. **Tx-pool / transaction-level DoS**: `transaction_fee` returns `DaoError::Overflow` because the truncated `maximum_withdraw` is less than `outputs_capacity`. The withdrawal transaction is rejected by every node and can never be committed, permanently locking the depositor's funds.

2. **Block-level DoS**: If a miner includes such a transaction, `dao_field_with_current_epoch` fails because `maximum_withdraws < input_capacities`, causing `withdrawed_interests` to underflow. The block is rejected by `DaoHeaderVerifier`. The miner loses the block reward and the depositor remains unable to withdraw.

In both cases the depositor's CKB is locked in the NervosDAO with no path to recovery, because the same arithmetic is used by every node uniformly.

---

### Likelihood Explanation

The overflow condition requires:

```
counted_capacity × withdrawing_ar / deposit_ar  >  u64::MAX
```

`ar` starts at `10_000_000_000` (10^10) and grows by `ar × g2 / c` per block — an extremely slow rate. For a cell with `counted_capacity` near `u64::MAX / 2` (~9.2 × 10^18 shannons ≈ 92 billion CKB), the ratio `withdrawing_ar / deposit_ar` needs to exceed ~2×, which would require the secondary issuance to roughly equal the total circulating supply — a multi-decade horizon under current parameters. For smaller deposits the threshold is proportionally higher. Likelihood is therefore **very low** under current economic parameters, but the bug is a latent correctness defect that grows more reachable as the chain ages and as large institutional depositors accumulate.

---

### Recommendation

Replace the silent cast with a checked conversion, consistent with every other `u128`→`u64` narrowing in the file:

```rust
// Before (silent truncation):
Capacity::shannons(withdraw_counted_capacity as u64)

// After (checked, returns DaoError::Overflow on overflow):
Capacity::shannons(
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
)
``` [8](#0-7) 

This aligns with the pattern already used for `ar_increase` and `miner_issuance` in the same file.

---

### Proof of Concept

**Trigger condition** (arithmetic):

```
deposit_ar  = 10_000_000_000_000_000   (ar after long chain history)
withdrawing_ar = 20_000_000_000_000_001  (ar doubled — ~decades later)
counted_capacity = 10_000_000_000_000_000_000  (10^19 shannons, ~100 billion CKB)

withdraw_counted_capacity (u128) =
    10_000_000_000_000_000_000 × 20_000_000_000_000_001
    / 10_000_000_000_000_000
  = 20_000_000_000_000_001_000  (> u64::MAX = 18_446_744_073_709_551_615)

withdraw_counted_capacity as u64 = 20_000_000_000_000_001_000 % 2^64
                                 = 1_553_255_926_290_449_384  (≪ counted_capacity)
```

**Step-by-step**:

1. A large holder deposits ~100 billion CKB into NervosDAO at block `D` (deposit_ar = 10^16).
2. Decades pass; `ar` doubles to ~2×10^16 at block `W`.
3. The holder initiates phase-1 withdrawal at block `W`.
4. The holder submits the phase-2 withdrawal transaction referencing block `W`.
5. Every node calls `calculate_maximum_withdraw`; `withdraw_counted_capacity` overflows `u64`; the truncated value (~1.55×10^18) is far below `counted_capacity` (~10^19).
6. `transaction_fee` computes `maximum_withdraw.safe_sub(outputs_capacity)` → underflow → `DaoError::Overflow`.
7. The transaction is rejected by every node's tx-pool. The depositor's funds are permanently inaccessible.

The existing test `check_withdraw_calculation_overflows` exercises the `safe_add` overflow path (line 156 right side) but does **not** exercise the `as u64` truncation path (line 156 left side), leaving this defect untested. [9](#0-8)

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

**File:** util/dao/src/lib.rs (L202-204)
```rust
        let reward128 = u128::from(target_g2.as_u64()) * u128::from(target_parent_u.as_u64())
            / u128::from(target_parent_c.as_u64());
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/src/lib.rs (L252-254)
```rust
        let current_s = parent_s
            .safe_add(nervosdao_issuance)
            .and_then(|s| s.safe_sub(withdrawed_interests))?;
```

**File:** util/dao/src/lib.rs (L256-258)
```rust
        let ar_increase128 =
            u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
        let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
```

**File:** util/dao/src/lib.rs (L330-332)
```rust
        maximum_withdraws
            .safe_sub(input_capacities)
            .map_err(Into::into)
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L300-319)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        let dao = DaoCalculator::new(
            &self.context.consensus,
            &self.context.store.borrow_as_data_loader(),
        )
        .dao_field(self.resolved.iter().map(AsRef::as_ref), self.parent)
        .map_err(|e| {
            error_target!(
                crate::LOG_TARGET,
                "Error generating dao data for block {}: {:?}",
                self.header.hash(),
                e
            );
            e
        })?;

        if dao != self.header.dao() {
            return Err((BlockErrorKind::InvalidDAO).into());
        }
        Ok(())
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
