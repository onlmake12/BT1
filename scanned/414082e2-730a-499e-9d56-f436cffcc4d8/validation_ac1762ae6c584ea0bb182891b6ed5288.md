### Title
Silent u128→u64 Truncation in DAO Withdrawal Capacity Calculation Causes Irrecoverable Fund Loss - (File: util/dao/src/lib.rs)

### Summary
`DaoCalculator::calculate_maximum_withdraw` computes the withdrawable capacity using a u128 intermediate value but casts it to u64 with a silent truncating `as u64` cast. When the intermediate result exceeds `u64::MAX` — which becomes reachable for large DAO deposits held over many years as the accumulation rate (AR) grows — the node silently discards the high bits. This causes the node to compute a maximum withdrawal smaller than the DAO script permits, resulting in either silent loss of earned interest or a permanently unwithdrawable prepare cell.

### Finding Description

In `calculate_maximum_withdraw` (`util/dao/src/lib.rs`):

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
``` [1](#0-0) 

The expression `withdraw_counted_capacity as u64` is a **silent truncating cast**. If `withdraw_counted_capacity` (u128) exceeds `u64::MAX`, the upper bits are silently discarded, producing an arbitrarily wrong (smaller) value `Y = withdraw_counted_capacity mod 2^64`.

This is inconsistent with every other u128→u64 narrowing in the same file, which all use the checked `u64::try_from(...).map_err(|_| DaoError::Overflow)?` pattern: [2](#0-1) [3](#0-2) 

The same buggy value is returned by the RPC `calculate_dao_maximum_withdraw`, so the user's wallet also computes the wrong (truncated) maximum and constructs a transaction claiming only the truncated amount. [4](#0-3) 

### Impact Explanation

Two distinct loss scenarios arise once `withdraw_counted_capacity > u64::MAX`:

**Scenario A – Silent fund loss**: `Y + occupied_capacity ≤ u64::MAX`. The `safe_add` succeeds. The node and RPC both report the truncated maximum. The user withdraws only `Y + occupied_capacity` shannons. The DAO script (dao.c) accepts this because the output is below the true maximum. The difference `(withdraw_counted_capacity - Y)` shannons is permanently unclaimable — the prepare cell has already been consumed.

**Scenario B – Permanent fund lock**: `Y + occupied_capacity > u64::MAX`. The `safe_add` returns `DaoError::Overflow`. Every withdrawal attempt is rejected by the node. The prepare cell is permanently unspendable; the user cannot recover any of their deposited capacity.

The `occupied_capacity` portion (the minimum cell storage cost, analogous to the original report's `WITHDRAWAL_STAKE`) is also irrecoverable in Scenario B. [5](#0-4) 

### Likelihood Explanation

The condition `withdraw_counted_capacity > u64::MAX` requires:

```
counted_capacity × withdrawing_ar / deposit_ar > 2^64 ≈ 1.844 × 10^19
```

The AR starts at `10^16` and grows each block by approximately:

```
ar_increase = parent_ar × current_g2 / parent_c
``` [6](#0-5) 

On mainnet, with secondary epoch reward ≈ 613,698,630 CKB/epoch and total capacity ≈ 33.6 billion CKB, AR grows by roughly `1.8 × 10^11` per block. For a depositor holding **10 billion CKB** (`counted_capacity ≈ 10^18 shannons`), the threshold is crossed after approximately **10 years** of continuous deposit. For a 1-billion-CKB holder, the threshold is crossed after roughly **110 years**. The likelihood is low in the near term but is a structural, time-growing risk for any large DAO depositor — a realistic class of CKB user.

The existing test `check_withdraw_calculation_overflows` only exercises the `safe_add` overflow path (Scenario B with a specific capacity near `u64::MAX`); it does not cover the silent-truncation path (Scenario A) where `Y + occupied_capacity` fits in u64. [7](#0-6) 

### Recommendation

Replace the silent cast with the checked conversion already used elsewhere in the same function:

```rust
// Before (buggy):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

// After (safe):
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
```

This makes the overflow explicit and consistent with `secondary_block_reward` and `dao_field_with_current_epoch`.

### Proof of Concept

**Setup**: A user deposits `counted_capacity = C` shannons into the DAO at block `B_d` (AR = `ar_d`). They submit a Phase-1 prepare transaction at block `B_w` (AR = `ar_w`).

**Trigger condition**: `C × ar_w / ar_d > u64::MAX`.

**Concrete numbers** (illustrative, not requiring mainnet state):
- `C = 10^18` shannons (10 billion CKB)
- `ar_d = 10^16` (genesis AR)
- `ar_w = 2 × 10^18` (AR after ~10 years of growth)
- `withdraw_counted_capacity = 10^18 × 2×10^18 / 10^16 = 2 × 10^20`
- `2 × 10^20 > u64::MAX (≈ 1.844 × 10^19)` ✓
- `Y = 2 × 10^20 mod 2^64 ≈ 1.553 × 10^19` (a plausible small value)
- Node reports maximum withdrawal = `Y + occupied_capacity` ≈ 1.553 × 10^19 shannons
- True maximum = `2 × 10^20 + occupied_capacity` shannons
- **Loss**: ≈ `1.847 × 10^20 - 1.553 × 10^19 ≈ 1.69 × 10^20` shannons ≈ 1.69 billion CKB silently lost

The attacker-controlled entry path is: any DAO depositor (unprivileged transaction sender / RPC caller) who deposits a sufficiently large amount and holds for a sufficiently long period. No privileged access, no Sybil attack, and no external dependency is required — the root cause is entirely within `util/dao/src/lib.rs`. [8](#0-7)

### Citations

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

**File:** util/dao/src/lib.rs (L204-204)
```rust
        let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
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
