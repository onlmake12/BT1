### Title
Silent u128→u64 Truncation in DAO Withdrawal Capacity Calculation — (`File: util/dao/src/lib.rs`)

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes `withdraw_counted_capacity` as a `u128` intermediate but casts it to `u64` with a bare `as u64` — a silent truncating cast. Every other analogous `u128→u64` narrowing in the same file uses `u64::try_from(...).map_err(|_| DaoError::Overflow)?`. When `withdraw_counted_capacity` exceeds `u64::MAX`, the high bits are silently discarded, returning a drastically under-counted withdrawal capacity with no error signal.

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

Every other `u128→u64` narrowing in the same file uses a checked conversion:

```rust
// line 204
let reward = u64::try_from(reward128).map_err(|_| DaoError::Overflow)?;
// line 245
Capacity::shannons(u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?)
// line 258
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
``` [2](#0-1) [3](#0-2) 

The formula is:

```
withdraw_counted_capacity = counted_capacity × withdrawing_ar / deposit_ar
```

`ar` (accumulation rate) starts at `10_000_000_000_000_000` (`10^16`) and grows monotonically with each block's secondary issuance. [4](#0-3) 

When `withdrawing_ar / deposit_ar` grows large enough that `counted_capacity × withdrawing_ar / deposit_ar > u64::MAX`, the `as u64` cast silently wraps the result to a tiny value. The function then returns `Ok(tiny_wrong_value)` instead of `Err(DaoError::Overflow)`.

The existing overflow test (`check_withdraw_calculation_overflows`) only catches the case where the *final* `safe_add(occupied_capacity)` overflows — it does not cover the case where `withdraw_counted_capacity` itself exceeds `u64::MAX` before the add, which would be silently swallowed. [5](#0-4) 

### Impact Explanation

`calculate_maximum_withdraw` is the authoritative function for determining how much a DAO depositor may withdraw. It is called:

1. **During block verification** via `DaoHeaderVerifier` → `dao_field_with_current_epoch` → `withdrawed_interests` → `transaction_maximum_withdraw` → `calculate_maximum_withdraw`. A silently truncated result causes `withdrawed_interests` to be under-counted, producing a wrong DAO field. Because both the miner and the verifier use the same buggy code path, the wrong field is self-consistent and passes `DaoHeaderVerifier`, permanently corrupting the on-chain DAO accounting. [6](#0-5) 

2. **For the depositor's withdrawal transaction**: the truncated maximum is used to validate the withdrawal output capacity. A depositor whose entitled amount exceeds the truncated value would have their withdrawal transaction rejected, permanently locking their interest (loss of funds).

3. **Via the `calculate_dao_maximum_withdraw` RPC**: callers receive a silently wrong (far too small) value, misleading wallets and tooling.

### Likelihood Explanation

The overflow condition requires:

```
counted_capacity × withdrawing_ar > u64::MAX × deposit_ar
```

`ar` starts at `10^16` and grows by `ar × g2 / C` per block (where `g2` is secondary issuance and `C` is total capacity). The total CKB supply is ~3.36 × 10^18 shannons, so `counted_capacity ≤ 3.36 × 10^18`. For the product to exceed `u64::MAX ≈ 1.84 × 10^19`:

```
withdrawing_ar / deposit_ar > 1.84×10^19 / 3.36×10^18 ≈ 5.5
```

`ar` must grow ~5.5× from genesis. At current secondary issuance rates this takes many decades. However:

- The bug is **latent and deterministic** — it will trigger as the chain ages.
- The code is **demonstrably inconsistent** with every other `u128→u64` narrowing in the same file.
- The existing test suite does not cover the silent-truncation path, meaning the defect is undetected by CI.
- A miner can include a DAO withdrawal transaction with a manipulated (too-large) `ar` in the header only if the `DaoHeaderVerifier` accepts it — but since both sides use the same buggy code, a crafted genesis or chain-spec with an inflated initial `ar` could trigger this immediately.

### Recommendation

Replace the bare `as u64` cast with the same checked pattern used everywhere else in the file:

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

Also add a dedicated unit test that sets `withdraw_counted_capacity > u64::MAX` (e.g., by using a very large `counted_capacity` and `withdrawing_ar >> deposit_ar`) and asserts `Err(DaoError::Overflow)`.

### Proof of Concept

**Trigger condition** (arithmetic):

```
deposit_ar    = 10_000_000_000_000_000   (genesis default)
withdrawing_ar = 55_000_000_000_000_000  (ar grew 5.5×)
counted_capacity = 3_360_000_000_000_000_000  (≈ total CKB supply in shannons)

withdraw_counted_capacity (u128)
  = 3_360_000_000_000_000_000 × 55_000_000_000_000_000
    / 10_000_000_000_000_000
  = 18_480_000_000_000_000_000
  > u64::MAX (18_446_744_073_709_551_615)

as u64 → 18_480_000_000_000_000_000 - 2^64
        = 33_255_926_290_448_384   ← silently wrong, ~0.18% of correct value
```

**Code path** (entry via RPC or block verification):

```
RPC: calculate_dao_maximum_withdraw
  → DaoCalculator::calculate_maximum_withdraw          [util/dao/src/lib.rs:127]
      → withdraw_counted_capacity as u64               [util/dao/src/lib.rs:156]  ← BUG

Block verification:
  ContextualBlockVerifier::verify
  → DaoHeaderVerifier::verify                          [contextual_block_verifier.rs:300]
  → DaoCalculator::dao_field                           [util/dao/src/lib.rs:270]
  → withdrawed_interests                               [util/dao/src/lib.rs:312]
  → transaction_maximum_withdraw                       [util/dao/src/lib.rs:38]
  → calculate_maximum_withdraw                         [util/dao/src/lib.rs:127]
      → withdraw_counted_capacity as u64               [util/dao/src/lib.rs:156]  ← BUG
``` [7](#0-6) [8](#0-7)

### Citations

**File:** util/dao/src/lib.rs (L127-159)
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
    }
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

**File:** util/dao/utils/src/lib.rs (L17-17)
```rust
const DEFAULT_GENESIS_ACCUMULATE_RATE: u64 = 10_000_000_000_000_000;
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

**File:** verification/contextual/src/contextual_block_verifier.rs (L300-320)
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
    }
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L670-671)
```rust
        if !self.switch.disable_daoheader() {
            DaoHeaderVerifier::new(&self.context, resolved, &parent, &block.header()).verify()?;
```
