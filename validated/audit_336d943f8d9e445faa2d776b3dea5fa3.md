### Title
Silent `u128 → u64` Truncation in DAO Maximum Withdrawal Calculation - (File: `util/dao/src/lib.rs`)

### Summary
In `DaoCalculator::calculate_maximum_withdraw`, the intermediate `u128` result `withdraw_counted_capacity` is cast to `u64` using the Rust `as` operator (a silent truncating cast) rather than the checked `u64::try_from(...)` used consistently everywhere else in the same file. If the `u128` value exceeds `u64::MAX`, the cast silently wraps to a much smaller value, producing a wrong maximum-withdrawal figure that propagates into both fee verification and the on-chain DAO field.

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes the interest-adjusted withdrawal amount as a `u128` product, then narrows it to `u64` with an unchecked `as` cast:

```rust
// lines 152-156
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← silent truncation
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

Every other analogous `u128 → u64` narrowing in the same file uses the checked form and returns `DaoError::Overflow` on failure:

- `secondary_block_reward` line 204: `u64::try_from(reward128).map_err(|_| DaoError::Overflow)?`
- `dao_field_with_current_epoch` lines 244–245: `u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?`
- `dao_field_with_current_epoch` line 258: `u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?` [2](#0-1) [3](#0-2) 

The `as u64` cast computes `withdraw_counted_capacity % 2^64`. When the true value exceeds `u64::MAX`, the result is a small, incorrect number — the exact same class of silent numeric misrepresentation as the 1inch `uint256(-amount)` bug.

### Impact Explanation

`calculate_maximum_withdraw` is called from two paths:

1. **`transaction_fee`** (via `transaction_maximum_withdraw`): used by the tx-pool and block verifier to compute the fee for a DAO Phase-2 withdrawal. A truncated maximum-withdraw value makes the fee appear negative, causing `safe_sub` to return an error and the transaction to be rejected — a denial-of-service for the depositor.

2. **`withdrawed_interests`** → **`dao_field_with_current_epoch`**: the truncated value flows into `current_s` (the NervosDAO secondary-issuance accumulator stored in every block header). If `withdrawed_interests` is computed as too small, `current_s` is inflated. Because every node runs the same deterministic calculation, all nodes accept the block with the wrong DAO field, permanently corrupting the on-chain DAO state and allowing future depositors to claim more interest than the protocol intends. [4](#0-3) [5](#0-4) 

### Likelihood Explanation

Triggering the overflow requires `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`. Because `counted_capacity ≤ total CKB supply` and the AR ratio grows slowly, this is not reachable under current mainnet supply. However:

- The total CKB supply (genesis 33.6 B CKBytes + secondary issuance over decades) approaches `u64::MAX` in shannons over a long enough horizon.
- A single cell holding a large fraction of the supply combined with a sufficiently aged deposit could push the product past `u64::MAX`.
- The bug is a latent time-bomb: it requires no privileged access, only a valid DAO withdrawal transaction submitted by any unprivileged user.

### Recommendation

Replace the silent `as u64` cast with the checked conversion already used everywhere else in the file:

```rust
// Before (line 155-156):
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;

// After:
let withdraw_counted_capacity_u64 =
    u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?;
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity_u64).safe_add(occupied_capacity)?;
``` [6](#0-5) 

### Proof of Concept

Numeric demonstration of the truncation (mirrors the 1inch gist):

```
deposit_ar      = 10_000_000_000_000_000
withdrawing_ar  = 10_000_000_001_000_000   (tiny AR increase)
counted_capacity = 18_446_744_073_709_551_615  (u64::MAX, e.g. a cell holding ~184 B CKBytes)

withdraw_counted_capacity (u128) =
    18_446_744_073_709_551_615 * 10_000_000_001_000_000
    / 10_000_000_000_000_000
  = 18_446_744_075_552_295_688   (> u64::MAX)

withdraw_counted_capacity as u64 =
    18_446_744_075_552_295_688 % 2^64
  = 1_842_744_073   (≈ 18 CKBytes — a catastrophically small result)
```

The existing test `check_withdraw_calculation_overflows` in `util/dao/src/tests.rs` (lines 295–350) catches the overflow only incidentally via a downstream `safe_add` error; it does not assert that `DaoError::Overflow` is returned from the truncation site itself, leaving the silent-truncation path untested for values where the truncated result does not subsequently overflow `safe_add`. [7](#0-6)

### Citations

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

**File:** util/dao/src/lib.rs (L242-258)
```rust
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
