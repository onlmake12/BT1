### Title
NervosDAO `s`-field Underflow from Rounding Inconsistency Between `ar` Accumulation and `nervosdao_issuance` Traps Depositor Funds - (File: `util/dao/src/lib.rs`)

---

### Summary

The NervosDAO accounting system maintains two entangled values — the accumulation rate `ar` (used to compute individual withdrawal interest) and the savings pool `s` (the total interest available for withdrawal) — using **different integer-division rounding paths**. This causes the sum of all individually claimable interests to exceed `s`, making `current_s.safe_sub(withdrawed_interests)` underflow and return `Err`. Because `DaoHeaderVerifier` rejects any block where `dao_field()` returns an error, a legitimate depositor's withdrawal transaction can never be included in a valid block, permanently trapping their funds.

---

### Finding Description

In `DaoCalculator::dao_field_with_current_epoch` (`util/dao/src/lib.rs`), two separate floor-division computations govern the same economic invariant but diverge due to rounding:

**Path 1 — `ar` accumulation (per block):** [1](#0-0) 

```
ar_increase = floor(parent_ar * g2 / c)
current_ar  = parent_ar + ar_increase
```

**Path 2 — `s` (savings pool) accumulation (per block):** [2](#0-1) 

```
miner_issuance    = floor(g2 * u / c)
nervosdao_issuance = g2 - miner_issuance
current_s          = parent_s + nervosdao_issuance - withdrawed_interests
```

**Path 3 — individual withdrawal interest (at withdrawal time):** [3](#0-2) 

```
withdraw_counted_capacity = floor(counted_capacity * withdrawing_ar / deposit_ar)
interest = withdraw_counted_capacity - counted_capacity
```

The invariant `s >= sum(all individual interests)` must hold for the subtraction on line 254 to succeed. Because `ar` is accumulated **multiplicatively** (each step multiplies by `1 + g2/c`, compounding rounding losses), while `s` is accumulated **additively** (each step adds `g2 - floor(g2*u/c)`), the two paths diverge. The compounded `ar` can yield an individual interest that exceeds the total `s` accumulated over the same period.

**Concrete minimal counterexample** (all values in shannons):

| Parameter | Value |
|---|---|
| `c` (total capacity) | 7 |
| `u` (occupied capacity) | 0 |
| `g2` (secondary issuance/block) | 3 |
| `ar_0` | 10,000,000,000,000,000 |
| Depositor `counted_capacity` | 7 |

**Block 1:**
- `ar_increase = floor(10^16 × 3 / 7) = 4,285,714,285,714,285`
- `ar_1 = 14,285,714,285,714,285`
- `nervosdao_issuance = 3 − floor(3×0/7) = 3` → `s_1 = 3`

**Block 2:**
- `ar_increase = floor(14,285,714,285,714,285 × 3 / 7) = 6,122,448,979,591,836`
- `ar_2 = 20,408,163,265,306,121`
- `nervosdao_issuance = 3` → `s_2 = 6`

**Withdrawal at block 2:**
- `withdraw_counted_capacity = floor(7 × 20,408,163,265,306,121 / 10^16) = floor(14.285...) = 14`
- `interest = 14 − 7 = 7`

**Result:** `s_2 = 6 < interest = 7` → `safe_sub` returns `Err(CapacityError::Overflow)` → `dao_field_with_current_epoch` propagates the error.

---

### Impact Explanation

`DaoHeaderVerifier::verify` calls `dao_field()` and propagates any error it returns: [4](#0-3) 

When `dao_field()` returns an error, the block is rejected. Because the error is deterministic (it depends only on the chain state and the withdrawal transaction), **every block that includes the affected withdrawal transaction will be rejected by every honest node**. The depositor's funds are permanently locked in the DAO cell with no valid path to withdrawal. This is a direct loss of user funds reachable without any privileged access.

---

### Likelihood Explanation

Any unprivileged user who deposits CKB into the NervosDAO and later attempts to withdraw is a potential victim. The discrepancy grows with:
- Smaller values of `c` relative to `g2` (higher rounding error per block)
- More elapsed blocks between deposit and withdrawal (compounding of `ar` rounding)
- Multiple depositors whose combined interests exceed `s`

On mainnet, `c` is very large (~2×10¹⁸ shannons), so the per-block discrepancy is tiny. However, the error accumulates over time and is amplified by the multiplicative compounding of `ar`. For small deposits held over long periods, or in edge cases where `c` is small (e.g., early chain state, devnet, or after a large portion of CKB is burned/locked), the invariant can be violated. The condition is passively triggered by normal user behavior — no adversarial action is required.

---

### Recommendation

1. **Immediate workaround**: In `dao_field_with_current_epoch`, replace the strict `safe_sub` with a saturating subtraction for `current_s`, clamping to zero rather than returning an error. This prevents fund trapping while accepting a 1-shannon rounding loss from `s`.

2. **Root fix**: Align the rounding of `ar` accumulation and `s` accumulation so they use the same mathematical path. Specifically, derive `nervosdao_issuance` from the same floor-division formula used to compute `ar_increase`, ensuring the two values remain consistent across all rounding scenarios. Alternatively, store only `ar` and derive `s` from it rather than maintaining them as independent accumulators.

3. **Invariant test**: Add a property-based test asserting `s >= sum(individual interests)` across many blocks and depositor configurations, including small `c` and large `g2` values.

---

### Proof of Concept

The underflow path in production code:

```
DaoCalculator::dao_field_with_current_epoch   [util/dao/src/lib.rs:209]
  └─ ar_increase = floor(parent_ar * g2 / c)  [line 257]       ← Path A
  └─ nervosdao_issuance = g2 - floor(g2*u/c)  [line 246]       ← Path B
  └─ current_s = parent_s + nervosdao_issuance
                 .safe_sub(withdrawed_interests)                 ← UNDERFLOW
                                                [line 252-254]

DaoHeaderVerifier::verify                      [contextual_block_verifier.rs:300]
  └─ dao_field() returns Err → block rejected  [line 305-314]
```

With the parameters from the counterexample above, after 2 blocks:
- `s = 6` shannons
- `withdrawed_interests = 7` shannons
- `safe_sub(7)` on `Capacity(6)` calls `6u64.checked_sub(7)` → `None` → `Err(CapacityError::Overflow)`
- Block containing the withdrawal is permanently invalid on all nodes
- Depositor's 7 shannons are trapped with no recovery path [5](#0-4) [1](#0-0) [6](#0-5) [4](#0-3)

### Citations

**File:** util/dao/src/lib.rs (L152-154)
```rust
        let withdraw_counted_capacity = u128::from(counted_capacity.as_u64())
            * u128::from(withdrawing_ar)
            / u128::from(deposit_ar);
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
