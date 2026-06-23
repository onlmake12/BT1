### Title
Unbounded Shift in `primary_epoch_reward()` Causes Reward Wrap-Around After 64 Halvings — (`spec/src/consensus.rs`)

---

### Summary

`primary_epoch_reward()` computes the block reward by right-shifting the initial reward by the number of halvings elapsed. No upper bound is enforced on the shift amount. In Rust, shifting a `u64` by ≥ 64 bits panics in debug builds and silently wraps (shift amount masked to 63 bits) in release builds. After 64 halvings the reward wraps back to the full initial value instead of reaching zero, breaking the protocol's deflationary issuance schedule and causing a consensus split between nodes compiled in different modes.

---

### Finding Description

In `spec/src/consensus.rs`:

```rust
pub fn primary_epoch_reward(&self, epoch_number: u64) -> Capacity {
    let halvings = epoch_number / self.primary_epoch_reward_halving_interval();
    Capacity::shannons(self.initial_primary_epoch_reward.as_u64() >> halvings)
}
```

`halvings` is an unbounded `u64`. When `halvings` reaches 64 the expression `initial_primary_epoch_reward.as_u64() >> halvings` exhibits two distinct failure modes:

| Build mode | Behaviour of `u64 >> 64` | Effect |
|---|---|---|
| **Debug** | Panic (`attempt to shift right with overflow`) | Node crash / DoS |
| **Release** | Rust masks shift to `halvings % 64 = 0`, result = full initial reward | Reward wraps back to `initial_primary_epoch_reward` |

The function is called at every halving boundary via `primary_epoch_reward_of_next_epoch()`, which feeds directly into the `EpochExt` built by `next_epoch_ext()` and stored on-chain. The analogous flaw in the external report is `level << 1` exceeding the 0–999 range of `rN`, making the condition always true; here `halvings` exceeds the 0–63 valid range of a `u64` shift, making the reward non-zero when it should be zero.

---

### Impact Explanation

At epoch 560,640 (mainnet, `halving_interval = 8760`) or epoch 64 (any chain with `halving_interval = 1`):

- **Release-mode nodes** compute `primary_epoch_reward = initial_primary_epoch_reward` (full reward) instead of 0. Every subsequent epoch reward is also wrong because `halvings % 64` cycles through 0–63 again.
- **Debug-mode nodes** panic and crash when processing the epoch boundary block.

The two populations of nodes disagree on the valid cellbase output capacity, causing a **consensus split**: release-mode nodes accept blocks that debug-mode nodes reject (or vice-versa), and all nodes diverge from the intended issuance schedule. Miners on the "wrap-around" fork receive unearned primary rewards, directly analogous to high-level heroes receiving guaranteed bonus rewards in the reference report.

---

### Likelihood Explanation

For mainnet (`halving_interval = 8760` epochs ≈ 4 years each), the trigger epoch is 560,640 ≈ 256 years away. For any chain configured with a small `halving_interval` (the parameter is freely configurable in the chain spec TOML and has no enforced minimum), the threshold is reached at epoch `64 × halving_interval`. A dev/test chain with `halving_interval = 1` reaches it at epoch 64. The code path is exercised automatically by the consensus engine at every halving boundary with no attacker action required beyond waiting; no privileged role is needed.

---

### Recommendation

Cap `halvings` before using it as a shift amount:

```rust
pub fn primary_epoch_reward(&self, epoch_number: u64) -> Capacity {
    let halvings = epoch_number / self.primary_epoch_reward_halving_interval();
    if halvings >= 64 {
        return Capacity::zero();
    }
    Capacity::shannons(self.initial_primary_epoch_reward.as_u64() >> halvings)
}
```

This mirrors Bitcoin Core's `GetBlockSubsidy`, which returns 0 once the shift amount reaches or exceeds 64.

---

### Proof of Concept

```rust
// Reproduces the wrap-around in release mode
let initial_reward: u64 = 1_917_808_21917808; // mainnet value
let halvings: u64 = 64;
// In release mode: 64 % 64 == 0, so result == initial_reward (not 0)
let reward = initial_reward >> halvings;
assert_eq!(reward, initial_reward); // passes in release, panics in debug
```

Relevant code locations: [1](#0-0) 

Called at every halving boundary via: [2](#0-1) 

Which feeds into the epoch extension built inside `next_epoch_ext()`: [3](#0-2) 

The halving interval constant (8760 for mainnet, configurable per chain spec): [4](#0-3)

### Citations

**File:** spec/src/consensus.rs (L79-80)
```rust
pub(crate) const DEFAULT_PRIMARY_EPOCH_REWARD_HALVING_INTERVAL: EpochNumber =
    4 * 365 * 24 * 60 * 60 / DEFAULT_EPOCH_DURATION_TARGET; // every 4 years
```

**File:** spec/src/consensus.rs (L670-673)
```rust
    pub fn primary_epoch_reward(&self, epoch_number: u64) -> Capacity {
        let halvings = epoch_number / self.primary_epoch_reward_halving_interval();
        Capacity::shannons(self.initial_primary_epoch_reward.as_u64() >> halvings)
    }
```

**File:** spec/src/consensus.rs (L940-956)
```rust
                        let primary_epoch_reward =
                            self.primary_epoch_reward_of_next_epoch(&epoch).as_u64();
                        let block_reward =
                            Capacity::shannons(primary_epoch_reward / next_epoch_length);
                        let remainder_reward =
                            Capacity::shannons(primary_epoch_reward % next_epoch_length);

                        let epoch_ext = EpochExt::new_builder()
                            .number(epoch.number() + 1)
                            .base_block_reward(block_reward)
                            .remainder_reward(remainder_reward)
                            .previous_epoch_hash_rate(adjusted_last_epoch_hash_rate)
                            .last_block_hash_in_previous_epoch(header.hash())
                            .start_number(header.number() + 1)
                            .length(next_epoch_length)
                            .compact_target(difficulty_to_compact(next_epoch_diff))
                            .build();
```

**File:** spec/src/consensus.rs (L991-997)
```rust
    fn primary_epoch_reward_of_next_epoch(&self, epoch: &EpochExt) -> Capacity {
        if !(epoch.number() + 1).is_multiple_of(self.primary_epoch_reward_halving_interval()) {
            epoch.primary_reward()
        } else {
            self.primary_epoch_reward(epoch.number() + 1)
        }
    }
```
