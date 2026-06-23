### Title
Unbounded Consensus Parameters `genesis_epoch_length`, `epoch_duration_target`, and `orphan_rate_target` Denominator Cause Integer Division-by-Zero Panic in Release Builds — (File: `spec/src/consensus.rs`)

### Summary

`build_genesis_epoch_ext` in `spec/src/consensus.rs` performs three integer divisions using chain-spec-supplied parameters (`genesis_epoch_length`, `epoch_duration_target`, and `genesis_orphan_rate.1`) without validating that any of them is non-zero. `ConsensusBuilder::build()` guards only `epoch_duration_target` with a `debug_assert!`, which is compiled out in every release binary. The `orphan_rate_target` denominator has no guard at all, and `ConsensusBuilder::orphan_rate_target` deliberately bypasses the safe constructor by calling `RationalU256::new_raw`. A node operator who sets any of these parameters to zero in the chain-spec TOML causes an unrecoverable panic during `ChainSpec::build_consensus()`, crashing the node at startup with no diagnostic error.

---

### Finding Description

**Root cause — three unguarded divisions in `build_genesis_epoch_ext`** [1](#0-0) 

```rust
let block_reward = Capacity::shannons(epoch_reward.as_u64() / genesis_epoch_length); // ① panics if 0
let remainder_reward = Capacity::shannons(epoch_reward.as_u64() % genesis_epoch_length);

let genesis_orphan_count =
    genesis_epoch_length * genesis_orphan_rate.0 as u64 / genesis_orphan_rate.1 as u64; // ② panics if denom 0
let genesis_hash_rate = compact_to_difficulty(compact_target)
    * (genesis_epoch_length + genesis_orphan_count)
    / epoch_duration_target; // ③ panics if 0
```

All three values come directly from the `Params` struct, which accepts any `u64` / `(u32, u32)` from the TOML file with no range check: [2](#0-1) 

**`debug_assert!` guards are compiled out in release builds**

`ConsensusBuilder::build()` guards `epoch_duration_target` and `initial_primary_epoch_reward` with `debug_assert!`: [3](#0-2) 

`debug_assert!` is a no-op in `--release` builds (every production binary). The `orphan_rate_target` denominator has **no guard at all**, not even a `debug_assert!`.

**`ConsensusBuilder::orphan_rate_target` bypasses the safe constructor**

`RationalU256::new` panics on a zero denominator; `new_raw` silently stores it: [4](#0-3) 

The builder uses `new_raw`: [5](#0-4) 

This means a zero denominator stored via `new_raw` propagates into `next_epoch_ext`, where `into_u256()` (`self.numer / self.denom`) would panic at the first epoch boundary even if startup somehow succeeded. [6](#0-5) 

**Call chain in `ChainSpec::build_consensus`** [7](#0-6) 

`build_genesis_epoch_ext` is called unconditionally before any validation, so the panic occurs at node startup.

---

### Impact Explanation

A node configured with `orphan_rate_target = [1, 0]`, `genesis_epoch_length = 0`, or `epoch_duration_target = 0` panics immediately on `ckb run` with an opaque integer-overflow/division-by-zero message and no recovery path. Because the `debug_assert!` guards are stripped in release builds, operators receive no compile-time or startup-time diagnostic — the node simply crashes. For a custom or dev network, this renders the chain permanently unbootable until the spec file is manually corrected, with no indication of which parameter is at fault.

---

### Likelihood Explanation

The `Params` struct is deserialized from a user-editable TOML file with `#[serde(deny_unknown_fields)]` but no value-range enforcement. Any operator of a custom/dev network who sets these parameters to zero (accidentally or through a misconfigured deployment script) triggers the crash. The mainnet and testnet specs are bundled and use safe defaults, so the risk is confined to custom networks. Likelihood is low but non-zero for dev/private deployments.

---

### Recommendation

1. Replace every `debug_assert!` in `ConsensusBuilder::build()` with a proper `assert!` or convert `build()` to return `Result<Consensus, Error>` so that invalid configurations are caught in release builds with a clear error message.
2. Add explicit lower-bound checks for `genesis_epoch_length > 0`, `epoch_duration_target > 0`, and `orphan_rate_target.1 > 0` inside `ChainSpec::build_consensus()` before calling `build_genesis_epoch_ext`.
3. Replace `RationalU256::new_raw` with `RationalU256::new` in `ConsensusBuilder::orphan_rate_target` so the safe constructor's zero-denominator guard is enforced.
4. Add the same bounds checks to the `Params` deserialization layer so that a malformed TOML produces a descriptive parse error rather than a runtime panic.

---

### Proof of Concept

Add the following line to any custom chain spec `[params]` section:

```toml
[params]
orphan_rate_target = [1, 0]   # denominator = 0
```

Run `ckb run`. `ChainSpec::build_consensus()` calls `build_genesis_epoch_ext(..., (1, 0))`. At line 226:

```rust
genesis_epoch_length * 1u64 / 0u64   // integer division by zero → panic
```

The node crashes immediately with `attempt to divide by zero` and no further diagnostic. The same crash is triggered by `genesis_epoch_length = 0` (line 222) or `epoch_duration_target = 0` (line 229), and in release builds the `debug_assert!` at line 343 that would have caught `epoch_duration_target = 0` is silently absent.

### Citations

**File:** spec/src/consensus.rs (L222-229)
```rust
    let block_reward = Capacity::shannons(epoch_reward.as_u64() / genesis_epoch_length);
    let remainder_reward = Capacity::shannons(epoch_reward.as_u64() % genesis_epoch_length);

    let genesis_orphan_count =
        genesis_epoch_length * genesis_orphan_rate.0 as u64 / genesis_orphan_rate.1 as u64;
    let genesis_hash_rate = compact_to_difficulty(compact_target)
        * (genesis_epoch_length + genesis_orphan_count)
        / epoch_duration_target;
```

**File:** spec/src/consensus.rs (L337-345)
```rust
        debug_assert!(
            self.inner.initial_primary_epoch_reward != Capacity::zero(),
            "initial_primary_epoch_reward must be non-zero"
        );

        debug_assert!(
            self.inner.epoch_duration_target() != 0,
            "epoch_duration_target must be non-zero"
        );
```

**File:** spec/src/consensus.rs (L387-393)
```rust
    pub fn orphan_rate_target(mut self, orphan_rate_target: (u32, u32)) -> Self {
        self.inner.orphan_rate_target = RationalU256::new_raw(
            U256::from(orphan_rate_target.0),
            U256::from(orphan_rate_target.1),
        );
        self
    }
```

**File:** spec/src/lib.rs (L182-251)
```rust
/// Parameters for CKB block chain
#[derive(Default, Clone, PartialEq, Eq, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Params {
    /// The initial_primary_epoch_reward
    ///
    /// See [`initial_primary_epoch_reward`](consensus/struct.Consensus.html#structfield.initial_primary_epoch_reward)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub initial_primary_epoch_reward: Option<Capacity>,
    /// The secondary_epoch_reward
    ///
    /// See [`secondary_epoch_reward`](consensus/struct.Consensus.html#structfield.secondary_epoch_reward)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub secondary_epoch_reward: Option<Capacity>,
    /// The max_block_cycles
    ///
    /// See [`max_block_cycles`](consensus/struct.Consensus.html#structfield.max_block_cycles)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_block_cycles: Option<Cycle>,
    /// The max_block_bytes
    ///
    /// See [`max_block_bytes`](consensus/struct.Consensus.html#structfield.max_block_bytes)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_block_bytes: Option<u64>,
    /// The cellbase_maturity
    ///
    /// See [`cellbase_maturity`](consensus/struct.Consensus.html#structfield.cellbase_maturity)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cellbase_maturity: Option<u64>,
    /// The primary_epoch_reward_halving_interval
    ///
    /// See [`primary_epoch_reward_halving_interval`](consensus/struct.Consensus.html#structfield.primary_epoch_reward_halving_interval)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub primary_epoch_reward_halving_interval: Option<EpochNumber>,
    /// The epoch_duration_target
    ///
    /// See [`epoch_duration_target`](consensus/struct.Consensus.html#structfield.epoch_duration_target)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub epoch_duration_target: Option<u64>,
    /// The genesis_epoch_length
    ///
    /// See [`genesis_epoch_length`](consensus/struct.Consensus.html#structfield.genesis_epoch_length)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub genesis_epoch_length: Option<BlockNumber>,
    /// The permanent_difficulty_in_dummy
    ///
    /// See [`permanent_difficulty_in_dummy`](consensus/struct.Consensus.html#structfield.permanent_difficulty_in_dummy)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub permanent_difficulty_in_dummy: Option<bool>,
    /// The max_block_proposals_limit
    ///
    /// See [`max_block_proposals_limit`](consensus/struct.Consensus.html#structfield.max_block_proposals_limit)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_block_proposals_limit: Option<u64>,
    /// The orphan_rate_target
    ///
    /// See [`orphan_rate_target`](consensus/struct.Consensus.html#structfield.orphan_rate_target)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub orphan_rate_target: Option<(u32, u32)>,
    /// The starting_block_limiting_dao_withdrawing_lock.
    ///
    /// See [`starting_block_limiting_dao_withdrawing_lock`](consensus/struct.Consensus.html#structfield.starting_block_limiting_dao_withdrawing_lock)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub starting_block_limiting_dao_withdrawing_lock: Option<u64>,
    /// The parameters for hard fork features.
    ///
    /// See [`hardfork_switch`](consensus/struct.Consensus.html#structfield.hardfork_switch)
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hardfork: Option<HardForkConfig>,
}
```

**File:** spec/src/lib.rs (L560-601)
```rust
    pub fn build_consensus(&self) -> Result<Consensus, Box<dyn Error>> {
        let hardfork_switch = self.build_hardfork_switch()?;
        let genesis_epoch_ext = build_genesis_epoch_ext(
            self.params.initial_primary_epoch_reward(),
            self.genesis.compact_target,
            self.params.genesis_epoch_length(),
            self.params.epoch_duration_target(),
            self.params.orphan_rate_target(),
        );
        let genesis_block = self.build_genesis()?;
        self.verify_genesis_hash(&genesis_block)?;

        let mut builder = ConsensusBuilder::new(genesis_block, genesis_epoch_ext)
            .id(self.name.clone())
            .cellbase_maturity(EpochNumberWithFraction::from_full_value(
                self.params.cellbase_maturity(),
            ))
            .secondary_epoch_reward(self.params.secondary_epoch_reward())
            .max_block_cycles(self.params.max_block_cycles())
            .max_block_bytes(self.params.max_block_bytes())
            .pow(self.pow.clone())
            .satoshi_pubkey_hash(self.genesis.satoshi_gift.satoshi_pubkey_hash.clone())
            .satoshi_cell_occupied_ratio(self.genesis.satoshi_gift.satoshi_cell_occupied_ratio)
            .primary_epoch_reward_halving_interval(
                self.params.primary_epoch_reward_halving_interval(),
            )
            .initial_primary_epoch_reward(self.params.initial_primary_epoch_reward())
            .epoch_duration_target(self.params.epoch_duration_target())
            .permanent_difficulty_in_dummy(self.params.permanent_difficulty_in_dummy())
            .max_block_proposals_limit(self.params.max_block_proposals_limit())
            .orphan_rate_target(self.params.orphan_rate_target())
            .starting_block_limiting_dao_withdrawing_lock(
                self.params.starting_block_limiting_dao_withdrawing_lock(),
            )
            .hardfork_switch(hardfork_switch);

        if let Some(deployments) = self.softfork_deployments() {
            builder = builder.softfork_deployments(deployments);
        }

        Ok(builder.build())
    }
```

**File:** util/rational/src/lib.rs (L34-47)
```rust
    pub fn new(numer: U256, denom: U256) -> RationalU256 {
        if denom.is_zero() {
            panic!("denominator == 0");
        }
        let mut ret = RationalU256::new_raw(numer, denom);
        ret.reduce();
        ret
    }

    /// Creates a new ratio `numer / denom` without checking whether `denom` is zero.
    #[inline]
    pub const fn new_raw(numer: U256, denom: U256) -> RationalU256 {
        RationalU256 { numer, denom }
    }
```

**File:** util/rational/src/lib.rs (L75-77)
```rust
    pub fn into_u256(self) -> U256 {
        self.numer / self.denom
    }
```
