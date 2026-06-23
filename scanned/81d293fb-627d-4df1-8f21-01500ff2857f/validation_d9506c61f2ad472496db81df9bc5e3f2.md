### Title
Silent `u128`→`u64` Truncating Cast in DAO Maximum Withdrawal Calculation Produces Wrong Capacity - (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the maximum withdrawable capacity for a DAO cell using a `u128` intermediate value, then casts it to `u64` with a bare `as u64` truncating cast. Unlike every other overflow-sensitive arithmetic operation in the same file — which uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?` — this cast is silent and is **not** protected by the workspace-level `overflow-checks = true` release profile setting. If the intermediate `u128` value exceeds `u64::MAX`, the function silently returns a wrong (truncated) capacity instead of propagating a `DaoError::Overflow`.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast silently truncates the high bits of `withdraw_counted_capacity` if it exceeds `u64::MAX`. Rust's `overflow-checks = true` (set in `[profile.release]`) only guards arithmetic operators (`+`, `-`, `*`); it has **no effect** on `as` casts. [2](#0-1) 

The same file already applies the correct pattern in `dao_field_with_current_epoch`:

```rust
let miner_issuance =
    Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?);
// ...
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [3](#0-2) 

`calculate_maximum_withdraw` was not updated with the same protection, creating an inconsistency.

---

### Impact Explanation

`calculate_maximum_withdraw` is called from `transaction_maximum_withdraw`, which feeds into two consensus-critical paths:

1. **`withdrawed_interests`** → **`dao_field_with_current_epoch`**: computes the DAO field embedded in every block header. A truncated value causes the DAO field to be computed incorrectly. Nodes that receive such a block will independently compute the correct DAO field and reject the block, causing a **consensus split / block invalidity** for the producing node. [4](#0-3) [5](#0-4) 

2. **`transaction_fee`**: returns a wrong fee for DAO withdrawal transactions, corrupting tx-pool fee accounting. [6](#0-5) 

3. **RPC `calculate_dao_maximum_withdraw`** (`rpc/src/module/experiment.rs`): returns a wrong maximum withdrawal amount to callers, potentially misleading wallets or tooling into constructing invalid transactions.

---

### Likelihood Explanation

For truncation to occur, `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`. Since `counted_capacity` is bounded by the total CKB supply (~3.36 × 10¹⁸ shannons, roughly 18% of `u64::MAX`), the `ar` ratio (`withdrawing_ar / deposit_ar`) would need to grow by approximately 5.5× from deposit to withdrawal. The `ar` accumulation rate grows very slowly under normal protocol operation, making this extremely unlikely on mainnet today. However, the defect is a real code inconsistency — the identical pattern was explicitly fixed elsewhere in the same function — and it would become reachable if the DAO accumulation rate ever grew significantly or if a large-capacity cell were deposited near the theoretical maximum.

---

### Recommendation

Replace the silent truncating cast with the same checked conversion already used in `dao_field_with_current_epoch`:

```rust
// Before (unsafe):
Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?

// After (safe, consistent with the rest of the file):
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?
``` [7](#0-6) 

---

### Proof of Concept

The existing test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` (line 296) demonstrates the overflow path but relies on the `safe_add` at the end to catch the error — it does **not** catch the silent truncation of `withdraw_counted_capacity`. A cell with:

- `output.capacity = 18_446_744_073_709_550_000` shannons (near `u64::MAX`)
- `withdrawing_ar / deposit_ar` slightly > 1 (e.g., `deposit_ar = 10_000_000_000_123_456`, `withdrawing_ar = 10_000_000_001_123_456`)

produces `withdraw_counted_capacity ≈ 18_446_744_073_711_382_xxx` which exceeds `u64::MAX`. The `as u64` cast silently truncates this to a small value (e.g., `~1_382_xxx`), and `safe_add(occupied_capacity)` succeeds, returning a drastically wrong capacity of ~14 shannons instead of `DaoError::Overflow`. [8](#0-7)

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

**File:** util/dao/src/lib.rs (L208-222)
```rust
    /// Calculates the new dao field with specified [`EpochExt`].
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
```

**File:** util/dao/src/lib.rs (L244-258)
```rust
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

**File:** Cargo.toml (L318-319)
```text
[profile.release]
overflow-checks = true
```

**File:** util/dao/src/tests.rs (L296-349)
```rust
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
```
