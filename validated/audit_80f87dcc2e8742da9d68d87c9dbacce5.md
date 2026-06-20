### Title
NervosDAO `current_s` Underflow via Rounding Divergence Between `nervosdao_issuance` and `ar_increase` — (File: `util/dao/src/lib.rs`)

---

### Summary

In `dao_field_with_current_epoch`, the `current_s` (NervosDAO secondary-issuance reserve) is incremented by `nervosdao_issuance` each block and decremented by `withdrawed_interests` on DAO withdrawals. Both quantities are derived from the same secondary issuance `current_g2` via integer (truncating) division, but they use **different divisors and different truncation directions**. When `parent_u` (occupied capacity) equals or is very close to `parent_c` (total capacity), `nervosdao_issuance` rounds to zero while `ar_increase` remains positive. The accumulation rate `ar` therefore grows, making depositors eligible for non-zero interest, yet `current_s` does not grow to match. The subsequent `safe_sub` call panics/errors, causing any block that includes a NervosDAO withdrawal to be rejected by `DaoHeaderVerifier`.

---

### Finding Description

In `util/dao/src/lib.rs`, `dao_field_with_current_epoch` computes two quantities from the same `current_g2`:

**`nervosdao_issuance`** (added to `current_s`): [1](#0-0) 

```rust
let miner_issuance128 = u128::from(current_g2.as_u64()) * u128::from(parent_u.as_u64())
    / u128::from(parent_c.as_u64());
let miner_issuance = Capacity::shannons(u64::try_from(miner_issuance128)...);
let nervosdao_issuance = current_g2.safe_sub(miner_issuance)?;
```

**`ar_increase`** (added to the accumulation rate `ar`): [2](#0-1) 

```rust
let ar_increase128 =
    u128::from(parent_ar) * u128::from(current_g2.as_u64()) / u128::from(parent_c.as_u64());
```

**`current_s` update** (the subtraction that can underflow): [3](#0-2) 

```rust
let current_s = parent_s
    .safe_add(nervosdao_issuance)
    .and_then(|s| s.safe_sub(withdrawed_interests))?;
```

**The divergence**: When `parent_u = parent_c`:

- `miner_issuance128 = floor(current_g2 * parent_c / parent_c) = current_g2`
- Therefore `nervosdao_issuance = current_g2 - current_g2 = 0` → `current_s` does **not** grow.
- But `ar_increase = floor(parent_ar * current_g2 / parent_c) > 0` → `ar` **does** grow.

Meanwhile, `withdrawed_interests` is computed in `calculate_maximum_withdraw` as: [4](#0-3) 

```rust
let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
    * u128::from(withdrawing_ar)
    / u128::from(deposit_ar);
```

Because `ar` grew (even though `current_s` did not), `withdrawing_ar > deposit_ar`, so `withdrawed_interests = floor(counted_capacity * withdrawing_ar / deposit_ar) - counted_capacity > 0`.

The result: `current_s = 0` (or near-zero) but `withdrawed_interests > 0`. The `safe_sub` at line 254 returns `Err(Overflow)`, propagating up through `dao_field` → `DaoHeaderVerifier::verify()` → block rejection. [5](#0-4) 

---

### Impact Explanation

Any block that includes a valid NervosDAO withdrawal transaction is **rejected at consensus** when `parent_u ≈ parent_c`. The miner who assembled such a block loses their block reward. More critically, if the condition `parent_u = parent_c` persists across consecutive blocks, **all NervosDAO withdrawals are permanently blocked** — no miner will include them because doing so causes their block to be orphaned. Depositors cannot reclaim their locked CKB.

---

### Likelihood Explanation

`parent_u = parent_c` is achievable without majority hashpower. Any user can create cells whose capacity equals exactly their occupied capacity (minimum-capacity cells). A miner can sustain the condition by issuing cellbase outputs with exactly the minimum required capacity, so that `added_occupied_capacities = block_reward` each block, keeping `current_u = current_c`. The condition does not require a Sybil attack, leaked keys, or social engineering — only normal on-chain transactions. The cost is locking capacity in minimum-capacity cells, which is recoverable.

---

### Recommendation

1. **Align rounding directions**: Ensure `nervosdao_issuance` is always at least as large as the interest that `ar_increase` entitles depositors to claim. One approach: compute `nervosdao_issuance` as `current_g2 - miner_issuance` using ceiling division for `miner_issuance` (i.e., round miner's share down, NervosDAO share up).
2. **Saturating subtraction**: Replace `safe_sub(withdrawed_interests)` with a saturating variant that clamps to zero rather than returning an error, analogous to the epsilon mitigation in the referenced Skale Manager fix.
3. **Invariant assertion**: Add a protocol-level invariant check that `parent_u <= parent_c` and that `current_s >= withdrawed_interests` before performing the subtraction, with a clear error path that does not reject the block.

---

### Proof of Concept

**Setup**: `parent_c = 10^16`, `parent_u = 10^16` (all capacity occupied), `current_g2 = 1`, `parent_ar = 10^16`.

**Block N computation**:
- `miner_issuance128 = 1 * 10^16 / 10^16 = 1` → `nervosdao_issuance = 1 - 1 = 0`
- `ar_increase128 = 10^16 * 1 / 10^16 = 1` → `ar` becomes `10^16 + 1`
- `current_s` remains `0`

**Depositor** deposited `counted_capacity = 10^16` shannons at `deposit_ar = 10^16`. Now withdraws at `withdrawing_ar = 10^16 + 1`:
- `withdraw_counted_capacity = 10^16 * (10^16 + 1) / 10^16 = 10^16 + 1`
- `withdrawed_interests = (10^16 + 1) - 10^16 = 1`

**Block N+1 verification** (`DaoHeaderVerifier`):
- `current_s.safe_sub(1)` → `0 - 1` → `Err(Overflow)` → **block rejected**

The withdrawal transaction is valid by all other checks, yet the block is deterministically rejected at `DaoHeaderVerifier::verify()`. [6](#0-5) [7](#0-6)

### Citations

**File:** util/dao/src/lib.rs (L152-156)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
        let withdraw_capacity =
            Capacity::shannons(withdraw_counted_capacity as u64).safe_add(occupied_capacity)?;
```

**File:** util/dao/src/lib.rs (L242-254)
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

**File:** verification/contextual/src/contextual_block_verifier.rs (L670-672)
```rust
        if !self.switch.disable_daoheader() {
            DaoHeaderVerifier::new(&self.context, resolved, &parent, &block.header()).verify()?;
        }
```
