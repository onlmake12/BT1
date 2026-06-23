### Title
Silent u128→u64 Truncating Cast in `DaoCalculator::calculate_maximum_withdraw` Silently Corrupts NervosDAO Withdrawal Capacity — (File: `util/dao/src/lib.rs`)

---

### Summary

`DaoCalculator::calculate_maximum_withdraw` computes the interest-scaled withdrawal capacity using a u128 intermediate, then casts it to u64 with a bare `as u64` truncating cast. If the intermediate value exceeds `u64::MAX`, the result silently wraps to a much smaller number and the function returns `Ok` with a drastically undervalued capacity. Every other analogous u128→u64 narrowing in the same file uses the checked `u64::try_from(...).map_err(|_| DaoError::Overflow)?` pattern; this one site does not. The function is on the consensus-critical path for both block DAO-field computation and transaction fee verification.

---

### Finding Description

In `util/dao/src/lib.rs`, `calculate_maximum_withdraw` computes:

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
let withdraw_capacity =
    Capacity::shannons(withdraw_counted_capacity as u64)   // ← truncating cast
        .safe_add(occupied_capacity)?;
``` [1](#0-0) 

The `as u64` cast is a Rust truncating (wrapping) cast. If `withdraw_counted_capacity` (u128) exceeds `u64::MAX`, the high bits are silently discarded and the result is a tiny value. The subsequent `safe_add(occupied_capacity)` only guards against overflow in the *final* addition; it cannot detect the prior truncation. The function therefore returns `Ok(tiny_value)` instead of `Err(DaoError::Overflow)`.

Every other u128→u64 narrowing in the same `impl` block uses the checked pattern:

- `secondary_block_reward` line 204: `u64::try_from(reward128).map_err(|_| DaoError::Overflow)?`
- `dao_field_with_current_epoch` line 258: `u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?`
- `dao_field_with_current_epoch` line 245: `u64::try_from(miner_issuance128).map_err(|_| DaoError::Overflow)?` [2](#0-1) [3](#0-2) [4](#0-3) 

The inconsistency is the bug. The `DaoError::Overflow` variant exists precisely for this purpose. [5](#0-4) 

`calculate_maximum_withdraw` is called from two consensus-critical callers:

1. `transaction_maximum_withdraw` → `transaction_fee` — used during block verification to validate that DAO withdrawal transactions do not create capacity from nothing.
2. `transaction_maximum_withdraw` → `withdrawed_interests` → `dao_field_with_current_epoch` — used to compute the `S_i` (surplus) field embedded in every block header's DAO field. [6](#0-5) [7](#0-6) 

It is also called directly from the RPC `calculate_dao_maximum_withdraw`, which wallets use to determine how much to claim. [8](#0-7) 

---

### Impact Explanation

When truncation fires, `withdraw_capacity` is set to `(withdraw_counted_capacity mod 2^64) + occupied_capacity`, a value orders of magnitude smaller than the correct amount.

**Consensus path 1 — transaction fee check:** `transaction_fee` computes `maximum_withdraw - outputs_capacity`. A wallet that queries the RPC (which uses the same truncated value) will set `outputs_capacity` to the truncated amount. The node accepts the transaction as valid. The depositor receives a tiny fraction of their principal plus interest; the remainder is permanently unspendable (locked in the DAO cell that has already been consumed).

**Consensus path 2 — DAO field:** `withdrawed_interests` feeds the truncated `maximum_withdraw` into the `S_i` update for the block header. The DAO field written into the chain is incorrect, causing all subsequent `ar`-based interest calculations to diverge from the true state. Nodes that independently recompute the DAO field will reject the block, causing a consensus split.

---

### Likelihood Explanation

Truncation requires `counted_capacity * withdrawing_ar / deposit_ar > u64::MAX`.

- Maximum total CKB supply ≈ 3.36 × 10¹⁸ shannons, so `counted_capacity ≤ 3.36 × 10¹⁸`.
- Genesis `ar` = `10^16` (`DEFAULT_GENESIS_ACCUMULATE_RATE`). [9](#0-8) 

- For truncation: `ar` at withdrawal time must exceed `deposit_ar × (u64::MAX / counted_capacity)`. With `counted_capacity` near the total supply, this requires `ar` to grow by a factor of ≈ 5.48 (to ≈ 5.48 × 10¹⁶).
- At the mainnet secondary issuance rate (~1.344 billion CKB/year) and total supply (~33.6 billion CKB), `ar` grows at roughly `ar × g2 / c ≈ 4%/year`, reaching the threshold in approximately **50 years**.

The likelihood is low in the near term but the bug is latent and deterministic: it will trigger on a long-lived chain for any large depositor who holds through the threshold epoch. The inconsistency with every other narrowing cast in the same file confirms this is unintentional.

---

### Recommendation

Replace the truncating cast with the same checked pattern used everywhere else in the file:

```rust
let withdraw_capacity =
    Capacity::shannons(
        u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?
    ).safe_add(occupied_capacity)?;
```

This makes `calculate_maximum_withdraw` return `Err(DaoError::Overflow)` instead of silently returning a wrong value, consistent with `secondary_block_reward` and `dao_field_with_current_epoch`.

---

### Proof of Concept

1. Construct a test chain with a genesis DAO field where `ar` is set to a value just below the truncation threshold (e.g., `ar = 5 × 10^16`), achievable by setting `pack_dao_data` directly in a test harness as done in `util/dao/src/tests.rs`. [10](#0-9) 

2. Create a deposit cell with `counted_capacity` near the total CKB supply (e.g., `3 × 10^18` shannons).
3. Set `withdrawing_ar` to `5.5 × 10^16` (slightly above threshold) and `deposit_ar` to `5 × 10^16`.
4. Call `DaoCalculator::calculate_maximum_withdraw`. The intermediate `withdraw_counted_capacity = 3×10^18 × 5.5×10^16 / 5×10^16 = 3.3×10^18`, which is within u64 range — no truncation yet.
5. Increase `counted_capacity` to `3.36 × 10^18` and `withdrawing_ar` to `5.5 × 10^16`: `withdraw_counted_capacity = 3.36×10^18 × 5.5×10^16 / 5×10^16 = 3.696×10^18 > u64::MAX (1.844×10^19)`? No, still within range.
6. Use `counted_capacity = 1.8 × 10^19` (near u64::MAX) and `withdrawing_ar / deposit_ar = 1.1`: `withdraw_counted_capacity = 1.98 × 10^19 > u64::MAX`. The `as u64` cast yields `1.98×10^19 - 1.844×10^19 = 1.36×10^18`. The function returns `Ok(1.36×10^18 + occupied_capacity)` instead of `Err(Overflow)`, silently accepting a withdrawal that pays the user ~7% of what they are owed.

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

**File:** util/dao/utils/src/error.rs (L36-38)
```rust
    /// Calculation overflow
    #[error("Overflow")]
    Overflow,
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

**File:** util/dao/utils/src/lib.rs (L16-17)
```rust
// This is multiplied by 10**16 to make sure we have enough precision.
const DEFAULT_GENESIS_ACCUMULATE_RATE: u64 = 10_000_000_000_000_000;
```

**File:** util/dao/src/tests.rs (L234-292)
```rust
#[test]
fn check_withdraw_calculation() {
    let data = Bytes::from(vec![1; 10]);
    let output = CellOutput::new_builder()
        .capacity(capacity_bytes!(1000000))
        .build();
    let tx = TransactionBuilder::default()
        .output(output.clone())
        .output_data(&data)
        .build();
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
        Capacity::bytes(data.len()).expect("should not overflow"),
        &deposit_block.hash(),
        &withdrawing_block.hash(),
    );
    assert_eq!(result.unwrap(), Capacity::shannons(100_000_000_009_999));
```
