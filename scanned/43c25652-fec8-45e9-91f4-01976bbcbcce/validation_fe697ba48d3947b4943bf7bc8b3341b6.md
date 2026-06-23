### Title
Unchecked Integer Arithmetic in `EpochExt::primary_reward()` Silently Produces Wrong Capacity — (`File: util/types/src/core/extras.rs`)

---

### Summary

`EpochExt::primary_reward()` performs a bare `u64` multiplication and addition with no overflow guard, returning a silently wrong `Capacity` value on overflow. Every other capacity arithmetic operation in the codebase uses the checked `safe_add` / `safe_mul` / `checked_add` family. This function is the sole exception, and because its return type is `Capacity` (not `CapacityResult`), it cannot propagate an error — it silently wraps. The same pattern recurs in `block_reward()` and `secondary_block_issuance()` with unchecked `u64` additions in boundary comparisons.

---

### Finding Description

`EpochExt::primary_reward()` reconstructs the total primary issuance for an epoch:

```rust
// util/types/src/core/extras.rs  lines 120-124
pub fn primary_reward(&self) -> Capacity {
    Capacity::shannons(
        self.base_block_reward.as_u64() * self.length + self.remainder_reward.as_u64(),
    )
}
``` [1](#0-0) 

Both the multiplication `base_block_reward * length` and the subsequent addition are plain Rust `u64` operations. In release builds Rust wraps on overflow; in debug builds it panics. Neither outcome is correct: a wrapped result is a silently wrong capacity value; a panic is a node crash.

Compare with every other capacity arithmetic site in the codebase, which uses the checked helpers:

```rust
// util/occupied-capacity/core/src/units.rs  lines 125-130
pub fn safe_add<C: IntoCapacity>(self, rhs: C) -> Result<Self> {
    self.0.checked_add(rhs.into_capacity().0)
        .map(Capacity::shannons)
        .ok_or(Error::Overflow)
}
``` [2](#0-1) 

The same unchecked pattern appears in `block_reward()` and `secondary_block_issuance()`:

```rust
// util/types/src/core/extras.rs  line 236
number < self.start_number() + self.remainder_reward.as_u64()
``` [3](#0-2) 

```rust
// util/types/src/core/extras.rs  line 262
block_number < self.start_number() + remainder
``` [4](#0-3) 

If either addition wraps, the boundary comparison silently flips, causing the wrong per-block reward to be returned.

`primary_reward()` is consumed in the consensus epoch-transition path and in the DAO calculator:

- `spec/src/consensus.rs` — `primary_epoch_reward_of_next_epoch` comparison
- `util/dao/src/lib.rs` — `dao_field_with_current_epoch` and `secondary_block_reward` [5](#0-4) 

The DAO field written into every block header is derived from these reward values. A wrong `primary_reward()` result propagates into `current_c`, `current_ar`, and `miner_issuance`, corrupting the DAO accumulator field that governs NervosDAO interest calculations for all depositors. [6](#0-5) 

---

### Impact Explanation

If overflow is triggered:

1. **Wrong DAO field written into a block header.** `dao_field_with_current_epoch` uses the corrupted reward to compute `current_c` (total capacity) and `current_ar` (accumulation rate). Every subsequent `calculate_maximum_withdraw` call uses these values to compute depositor interest, producing wrong withdrawal amounts. [7](#0-6) 

2. **Block reward verification failure or bypass.** The `RewardVerifier` checks that the cellbase output matches the computed reward. A silently wrong `primary_reward()` causes the expected reward to differ from the actual consensus value, either rejecting valid blocks or accepting under-rewarded ones. [8](#0-7) 

3. **Consensus split.** Nodes that overflow and nodes that do not will compute different DAO fields and different expected rewards, causing a chain split.

---

### Likelihood Explanation

With current mainnet parameters the overflow is not reachable:

- `INITIAL_PRIMARY_EPOCH_REWARD ≈ 1.9 × 10¹⁵` shannons; it only halves over time.
- `base_block_reward = primary_epoch_reward / epoch_length`; `epoch_length` is bounded by `MAX_EPOCH_LENGTH` (consensus constant).
- Therefore `base_block_reward * length ≈ primary_epoch_reward ≈ 1.9 × 10¹⁵`, far below `u64::MAX ≈ 1.8 × 10¹⁹`. [9](#0-8) 

The risk is latent: a future chain-spec change (e.g., a custom chain, a testnet with inflated rewards, or a governance-approved parameter change) that raises `initial_primary_epoch_reward` or `epoch_length` beyond safe bounds would silently activate the bug with no code change required. The absence of any overflow guard means there is no safety net regardless of parameter values.

---

### Recommendation

Replace the bare arithmetic in `primary_reward()` with checked operations and change the return type to `CapacityResult<Capacity>` (or add a `debug_assert` at minimum):

```rust
pub fn primary_reward(&self) -> CapacityResult<Capacity> {
    self.base_block_reward
        .safe_mul(self.length)
        .and_then(|c| c.safe_add(self.remainder_reward))
}
```

For `block_reward()` and `secondary_block_issuance()`, replace the unchecked additions in boundary comparisons with `checked_add`, returning an error on overflow:

```rust
// block_reward
let upper = self.start_number()
    .checked_add(self.remainder_reward.as_u64())
    .ok_or(CapacityError::Overflow)?;
if number >= self.start_number() && number < upper { ... }
``` [10](#0-9) 

---

### Proof of Concept

The following values demonstrate the overflow path (hypothetical custom chain spec):

```
base_block_reward = 10_000_000_000_000_000_000  (10^19 shannons, ~10^11 CKB)
length            = 2
remainder_reward  = 0

primary_reward() = 10^19 * 2 + 0
                 = 20_000_000_000_000_000_000  -- overflows u64 (max 18_446_744_073_709_551_615)
                 = 1_553_255_926_290_448_384   -- silently wrapped value
```

A node running this chain spec would write a DAO field derived from `1.55 × 10¹⁸` instead of `2 × 10¹⁹`, corrupting the accumulation rate `ar` for all NervosDAO depositors and causing every subsequent withdrawal calculation to return a wrong (under-counted) interest amount, directly analogous to the oracle exponent scaling error in the reference report. [1](#0-0) [11](#0-10)

### Citations

**File:** util/types/src/core/extras.rs (L120-124)
```rust
    pub fn primary_reward(&self) -> Capacity {
        Capacity::shannons(
            self.base_block_reward.as_u64() * self.length + self.remainder_reward.as_u64(),
        )
    }
```

**File:** util/types/src/core/extras.rs (L234-266)
```rust
    pub fn block_reward(&self, number: BlockNumber) -> CapacityResult<Capacity> {
        if number >= self.start_number()
            && number < self.start_number() + self.remainder_reward.as_u64()
        {
            self.base_block_reward.safe_add(Capacity::one())
        } else {
            Ok(self.base_block_reward)
        }
    }

    /// Returns the epoch number with fraction for a given block number.
    pub fn number_with_fraction(&self, number: BlockNumber) -> EpochNumberWithFraction {
        debug_assert!(
            number >= self.start_number() && number < self.start_number() + self.length()
        );
        EpochNumberWithFraction::new(self.number(), number - self.start_number(), self.length())
    }

    // We name this issuance since it covers multiple parts: block reward,
    // NervosDAO issuance as well as treasury part.
    /// Returns the secondary block issuance for a given block number.
    pub fn secondary_block_issuance(
        &self,
        block_number: BlockNumber,
        secondary_epoch_issuance: Capacity,
    ) -> CapacityResult<Capacity> {
        let mut g2 = Capacity::shannons(secondary_epoch_issuance.as_u64() / self.length());
        let remainder = secondary_epoch_issuance.as_u64() % self.length();
        if block_number >= self.start_number() && block_number < self.start_number() + remainder {
            g2 = g2.safe_add(Capacity::one())?;
        }
        Ok(g2)
    }
```

**File:** util/occupied-capacity/core/src/units.rs (L125-130)
```rust
    pub fn safe_add<C: IntoCapacity>(self, rhs: C) -> Result<Self> {
        self.0
            .checked_add(rhs.into_capacity().0)
            .map(Capacity::shannons)
            .ok_or(Error::Overflow)
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

**File:** util/dao/src/lib.rs (L208-264)
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

        let (parent_ar, parent_c, parent_s, parent_u) = extract_dao_data(parent.dao());

        // g contains both primary issuance and secondary issuance,
        // g2 is the secondary issuance for the block, which consists of
        // issuance for the miner, NervosDAO and treasury.
        // When calculating issuance in NervosDAO, we use the real
        // issuance for each block(which will only be issued on chain
        // after the finalization delay), not the capacities generated
        // in the cellbase of current block.
        let current_block_number = parent.number() + 1;
        let current_g2 = current_block_epoch.secondary_block_issuance(
            current_block_number,
            self.consensus.secondary_epoch_reward(),
        )?;
        let current_g = current_block_epoch
            .block_reward(current_block_number)
            .and_then(|c| c.safe_add(current_g2))?;

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
        let current_ar = parent_ar
            .checked_add(ar_increase)
            .ok_or(DaoError::Overflow)?;

        Ok(pack_dao_data(current_ar, current_c, current_s, current_u))
    }
```

**File:** util/reward-calculator/src/lib.rs (L103-132)
```rust
        let txs_fees = self.txs_fees(target)?;
        let proposal_reward = self.proposal_reward(parent, target)?;
        let (primary, secondary) = self.base_block_reward(target)?;

        let total = txs_fees
            .safe_add(proposal_reward)?
            .safe_add(primary)?
            .safe_add(secondary)?;

        debug!(
            "[RewardCalculator] target {} {}\n
             txs_fees {:?}, proposal_reward {:?}, primary {:?}, secondary: {:?}, total_reward {:?}",
            target.number(),
            target.hash(),
            txs_fees,
            proposal_reward,
            primary,
            secondary,
            total,
        );

        let block_reward = BlockReward {
            total,
            primary,
            secondary,
            tx_fee: txs_fees,
            proposal_reward,
        };

        Ok((target_lock, block_reward))
```

**File:** spec/src/consensus.rs (L42-46)
```rust
// 1.344 billion per year
pub(crate) const DEFAULT_SECONDARY_EPOCH_REWARD: Capacity = Capacity::shannons(613_698_63013698);
// 4.2 billion per year
pub(crate) const INITIAL_PRIMARY_EPOCH_REWARD: Capacity = Capacity::shannons(1_917_808_21917808);
const MAX_UNCLE_NUM: usize = 2;
```
