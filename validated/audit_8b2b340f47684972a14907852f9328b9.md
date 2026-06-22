### Title
`dao_field_with_current_epoch` Monotonically-Growing u64 Accumulators Overflow Without Graceful Handling, Breaking Chain Liveness and Permanently Blocking NervosDAO Withdrawals — (`File: util/dao/src/lib.rs`)

---

### Summary

`dao_field_with_current_epoch` computes two monotonically-growing u64 accumulators — `current_c` (total CKB capacity) and `current_ar` (accumulation rate) — using checked arithmetic that returns `DaoError::Overflow` on overflow rather than handling it gracefully. This error propagates directly into `DaoHeaderVerifier::verify()` during block verification and into `calc_dao` during block assembly. When overflow occurs, every subsequent block fails verification, halting the chain and permanently preventing NervosDAO withdrawals from being processed.

---

### Finding Description

In `dao_field_with_current_epoch`, two u64 accumulators grow without bound:

**`current_c` (total CKB capacity):** [1](#0-0) 

```rust
let current_c = parent_c.safe_add(current_g)?;
```

`current_c` grows by the full block reward (`primary + secondary issuance`) every block. `safe_add` returns `DaoError::Overflow` (via `CapacityError::Overflow`) when the u64 sum exceeds `u64::MAX`.

**`current_ar` (accumulation rate):** [2](#0-1) 

```rust
let ar_increase128 =
    u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
let ar_increase = u64::try_from(ar_increase128).map_err(|_| DaoError::Overflow)?;
let current_ar = parent_ar
    .checked_add(ar_increase)
    .ok_or(DaoError::Overflow)?;
```

`parent_ar` starts at `10_000_000_000_000_000` (10^16) and grows by approximately `ar * g2 / c` per block. Both `u64::try_from` and `checked_add` return `DaoError::Overflow` on overflow.

The `DaoError` type confirms these are the only two overflow variants: [3](#0-2) 

This error propagates unhandled through two critical callers:

**Block verification** — `DaoHeaderVerifier::verify()`: [4](#0-3) 

The `?` operator converts `DaoError::Overflow` into a hard `Error`, causing `ContextualBlockVerifier::verify()` to reject the block: [5](#0-4) 

**Block assembly** — `calc_dao` in the block assembler: [6](#0-5) 

The `?` propagates the overflow error, causing block template generation to fail.

There is no graceful fallback — no saturation, no capping, no "stop accumulating" path analogous to what the Euler fix introduced for `initVaultCache`.

A secondary inconsistency exists in `calculate_maximum_withdraw`: the result of the u128 multiplication is cast with a **silent truncating** `as u64` rather than a checked `u64::try_from()`: [7](#0-6) 

This is inconsistent with the explicit overflow handling elsewhere and could silently produce incorrect (lower) withdrawal amounts if `ar` grows large enough.

---

### Impact Explanation

When `current_c` or `current_ar` overflows u64:

1. `dao_field_with_current_epoch` returns `DaoError::Overflow`.
2. `DaoHeaderVerifier::verify()` propagates this as a hard block error — every block from that point forward fails verification.
3. The chain halts: no new blocks can be appended.
4. NervosDAO withdrawals, which must be included in blocks, become permanently impossible.
5. Block assembly also fails via `calc_dao`, so miners cannot produce valid templates either.

This is a complete, irreversible chain liveness failure with no on-chain recovery path.

---

### Likelihood Explanation

The overflow is not attacker-acceleratable — `current_c` and `current_ar` are protocol-determined and verified by consensus at every block. No external actor can inject inflated values.

Estimated timelines based on mainnet parameters (genesis `c ≈ 3.36 × 10^18` shannons, secondary issuance ≈ `1.344 × 10^9` CKB/year = `1.344 × 10^17` shannons/year, `u64::MAX ≈ 1.844 × 10^19` shannons):

- **`current_c` overflow**: approximately 87–112 years from genesis.
- **`current_ar` overflow**: approximately 546 years from genesis (starts at `10^16`, grows ~4% per year).

Likelihood is **very low** in the near term. However, the structural defect is real and confirmed by the existing test: [8](#0-7) 

which explicitly demonstrates that `dao_field` returns `Overflow` when `current_c` is near `u64::MAX` — confirming the code path is reachable and unhandled.

---

### Recommendation

1. **Replace hard overflow errors with saturating/capping behavior** for `current_c` and `current_ar`. When `current_c` would overflow, cap it at `u64::MAX` (issuance effectively stops). When `current_ar` would overflow, cap it at `u64::MAX` (interest rate stops growing). This mirrors the Euler fix: "the accumulator will stop growing, meaning that no further interest will be earned/charged. However, debts can still be repaid and funds withdrawn."

2. **Fix the silent truncation** in `calculate_maximum_withdraw` at line 156: replace `withdraw_counted_capacity as u64` with `u64::try_from(withdraw_counted_capacity).map_err(|_| DaoError::Overflow)?` to be consistent with the rest of the codebase.

3. **Add inline documentation** in `dao_field_with_current_epoch` explaining the overflow behavior and the chosen handling strategy, so future developers and auditors understand the design intent.

---

### Proof of Concept

The existing test at `util/dao/src/tests.rs:156–177` already demonstrates the reachable overflow path: [9](#0-8) 

Setting `parent_c` to `18_446_744_073_709_000_000` (near `u64::MAX`) causes `dao_field` to return `DaoError::Overflow`. In production, once `current_c` reaches this threshold through normal block issuance, `DaoHeaderVerifier::verify()` will propagate this error for every subsequent block, halting the chain and permanently blocking all NervosDAO withdrawals.

### Citations

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** util/dao/src/lib.rs (L248-248)
```rust
        let current_c = parent_c.safe_add(current_g)?;
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

**File:** util/dao/utils/src/error.rs (L36-41)
```rust
    /// Calculation overflow
    #[error("Overflow")]
    Overflow,
    /// ZeroC
    #[error("ZeroC")]
    ZeroC,
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L300-314)
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
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L670-672)
```rust
        if !self.switch.disable_daoheader() {
            DaoHeaderVerifier::new(&self.context, resolved, &parent, &block.header()).verify()?;
        }
```

**File:** tx-pool/src/block_assembler/mod.rs (L677-678)
```rust
        let dao = DaoCalculator::new(consensus, &snapshot.borrow_as_data_loader())
            .dao_field_with_current_epoch(entries_iter, tip_header, current_epoch)?;
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
