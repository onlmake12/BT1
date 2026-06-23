### Title
Critical Consensus Parameter Validation Bypassed in Release Builds via `debug_assert!` — (`spec/src/consensus.rs`)

### Summary
`ConsensusBuilder::build()` guards all critical consensus parameter invariants exclusively with `debug_assert!`. Because `debug_assert!` is unconditionally compiled out in release builds (`--release`), every production CKB binary ships with zero enforcement of these invariants. A custom chain spec (accepted by the local CLI) that sets `genesis_epoch_length`, `epoch_duration_target`, or `initial_primary_epoch_reward` to zero will pass through `build_consensus()` silently and then trigger an integer divide-by-zero panic inside `build_genesis_epoch_ext()`, crashing the node at startup.

### Finding Description

`ConsensusBuilder::build()` contains four `debug_assert!` guards that are the **only** enforcement of critical consensus invariants: [1](#0-0) 

```rust
pub fn build(mut self) -> Consensus {
    debug_assert!(self.inner.genesis_block.difficulty() > U256::zero(), ...);
    debug_assert!(!self.inner.genesis_block.data().transactions().is_empty() && ..., ...);
    debug_assert!(self.inner.initial_primary_epoch_reward != Capacity::zero(), ...);
    debug_assert!(self.inner.epoch_duration_target() != 0, ...);
    ...
}
```

`debug_assert!` expands to nothing in release mode. There is no `assert!`, no `Result`-returning error, and no upstream validation in `Params` or `build_consensus()`.

The `Params` struct accepts all of these values directly from a TOML file with no range checks: [2](#0-1) 

`build_consensus()` passes them straight into `build_genesis_epoch_ext()`: [3](#0-2) 

Inside `build_genesis_epoch_ext()`, three integer divisions are performed against the unvalidated parameters with no zero-guard: [4](#0-3) 

```rust
let block_reward   = Capacity::shannons(epoch_reward.as_u64() / genesis_epoch_length); // ← div/0
let remainder_reward = Capacity::shannons(epoch_reward.as_u64() % genesis_epoch_length); // ← div/0
let genesis_orphan_count = genesis_epoch_length * genesis_orphan_rate.0 as u64
                           / genesis_orphan_rate.1 as u64;                              // ← div/0
let genesis_hash_rate = ... / epoch_duration_target;                                    // ← div/0
```

### Impact Explanation

A release-mode CKB node that loads a chain spec containing `genesis_epoch_length = 0` or `epoch_duration_target = 0` will panic with an integer overflow/divide-by-zero during startup, before any block is processed. The node is completely non-functional. Because the `debug_assert!` guards are stripped, there is no error message, no graceful rejection — only a hard crash. For `initial_primary_epoch_reward = 0`, the node starts silently with a zero-reward consensus, producing incorrect epoch reward calculations for all subsequent blocks.

### Likelihood Explanation

The `ckb init --chain dev --import-spec <file>` CLI path is explicitly supported and documented. Any local CLI user who supplies a crafted or accidentally misconfigured spec file with a zero value for any of these parameters will trigger the crash in a release binary. The `Params` struct derives `Default` and all fields are `Option<T>`, so a spec that omits a field falls back to a safe default — but a spec that explicitly sets a field to `0` is accepted without complaint and forwarded directly to the dividing code.

### Recommendation

Replace all four `debug_assert!` calls in `ConsensusBuilder::build()` with proper `assert!` (or convert `build()` to return `Result<Consensus, SpecError>`) so that the invariants are enforced in release builds. Additionally, add explicit zero-checks in `build_genesis_epoch_ext()` before each division, and add range validation in `Params`'s accessor methods (`genesis_epoch_length()`, `epoch_duration_target()`, `orphan_rate_target()`) to reject zero denominators at deserialization time.

### Proof of Concept

1. Create `bad_spec.toml` identical to `resource/specs/dev.toml` but with `genesis_epoch_length = 0` added under `[params]`.
2. Run `ckb init --chain dev --import-spec bad_spec.toml --force`.
3. Run the **release** binary: `ckb run`.
4. The process panics immediately in `build_genesis_epoch_ext` at the division `epoch_reward.as_u64() / genesis_epoch_length` (line 222), because `debug_assert!` on line 337–340 was compiled out and never fired.
5. Repeat with `epoch_duration_target = 0` to trigger the division at line 229. [5](#0-4) [6](#0-5)

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

**File:** spec/src/consensus.rs (L318-345)
```rust
    pub fn build(mut self) -> Consensus {
        debug_assert!(
            self.inner.genesis_block.difficulty() > U256::zero(),
            "genesis difficulty should greater than zero"
        );
        debug_assert!(
            !self.inner.genesis_block.data().transactions().is_empty()
                && !self
                    .inner
                    .genesis_block
                    .data()
                    .transactions()
                    .get(0)
                    .unwrap()
                    .witnesses()
                    .is_empty(),
            "genesis block must contain the witness for cellbase"
        );

        debug_assert!(
            self.inner.initial_primary_epoch_reward != Capacity::zero(),
            "initial_primary_epoch_reward must be non-zero"
        );

        debug_assert!(
            self.inner.epoch_duration_target() != 0,
            "epoch_duration_target must be non-zero"
        );
```

**File:** spec/src/lib.rs (L183-251)
```rust
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

**File:** spec/src/lib.rs (L562-568)
```rust
        let genesis_epoch_ext = build_genesis_epoch_ext(
            self.params.initial_primary_epoch_reward(),
            self.genesis.compact_target,
            self.params.genesis_epoch_length(),
            self.params.epoch_duration_target(),
            self.params.orphan_rate_target(),
        );
```
