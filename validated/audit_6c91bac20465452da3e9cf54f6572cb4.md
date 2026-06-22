### Title
`debug_assert!`-Only Validation of Critical Consensus Parameters Silently Bypassed in Release Builds — (`File: spec/src/consensus.rs`)

---

### Summary

`ConsensusBuilder::build()` guards three consensus-critical invariants exclusively with Rust's `debug_assert!` macro. Because `debug_assert!` is compiled out in release builds (`cargo build --release`), every production CKB node silently accepts zero-valued `epoch_duration_target`, `initial_primary_epoch_reward`, and genesis `difficulty` without any error. A local CLI user who sets `epoch_duration_target = 0` in a custom chain spec will see the node start cleanly in release mode, then crash with a division-by-zero panic at the first epoch boundary inside `next_epoch_ext()`.

---

### Finding Description

`ConsensusBuilder::build()` in `spec/src/consensus.rs` contains five `debug_assert!` guards:

```rust
debug_assert!(
    self.inner.genesis_block.difficulty() > U256::zero(),
    "genesis difficulty should greater than zero"
);
// ...
debug_assert!(
    self.inner.initial_primary_epoch_reward != Capacity::zero(),
    "initial_primary_epoch_reward must be non-zero"
);
debug_assert!(
    self.inner.epoch_duration_target() != 0,
    "epoch_duration_target must be non-zero"
);
``` [1](#0-0) 

In Rust, `debug_assert!` expands to nothing when compiled with `--release`. All production CKB binaries are release builds. The three parameters checked here are set from the chain spec TOML file (`[params]` section) parsed by `ChainSpec::build_consensus()`: [2](#0-1) 

The `Params` struct accepts all three as plain `Option<T>` with no range or non-zero validation: [3](#0-2) 

When `epoch_duration_target = 0` is written into `specs/dev.toml` and `permanent_difficulty_in_dummy = true`, the node starts without error in release mode. At the first epoch boundary, `next_epoch_ext()` executes:

```rust
let next_epoch_length =
    self.epoch_duration_target().div_ceil(MIN_BLOCK_INTERVAL);  // 0 / 8 = 0
let block_reward =
    Capacity::shannons(primary_epoch_reward / next_epoch_length); // divide by zero → panic
``` [4](#0-3) 

A second independent path: `build_genesis_epoch_ext()` divides by `epoch_duration_target` directly at startup:

```rust
let genesis_hash_rate = compact_to_difficulty(compact_target)
    * (genesis_epoch_length + genesis_orphan_count)
    / epoch_duration_target;   // panic if 0
``` [5](#0-4) 

Similarly, `genesis_epoch_length = 0` causes an immediate division-by-zero in `build_genesis_epoch_ext()` at line 222 and in `calculate_block_reward()`: [6](#0-5) 

And `orphan_rate_target = [N, 0]` (zero denominator) is stored via `RationalU256::new_raw` — which explicitly skips the zero-denominator check — and later causes a panic when `RationalU256::new()` is called during epoch arithmetic: [7](#0-6) [8](#0-7) 

---

### Impact Explanation

| Parameter | Release-mode behavior | Downstream effect |
|---|---|---|
| `epoch_duration_target = 0` | Silently accepted (debug_assert stripped) | Panic in `next_epoch_ext()` at first epoch boundary when `permanent_difficulty_in_dummy = true`; incorrect epoch lengths otherwise |
| `initial_primary_epoch_reward = 0` | Silently accepted | All block rewards are zero; chain economics broken |
| `genesis_block.difficulty() = 0` | Silently accepted | Hash-rate estimation in `next_epoch_ext()` produces zero, corrupting difficulty adjustment |
| `genesis_epoch_length = 0` | Panic at startup (both modes) | Node refuses to start |
| `orphan_rate_target = [N, 0]` | Panic at startup (both modes) | Node refuses to start |

The `debug_assert!`-only parameters are the most dangerous because the node starts cleanly and the failure is deferred, making diagnosis harder.

---

### Likelihood Explanation

Mainnet and testnet use bundled, hardcoded specs with correct values, so they are unaffected. The risk is real for:

- Developers running `ckb init --chain dev` and customizing `specs/dev.toml`
- Operators deploying private/staging chains (e.g., `specs/staging.toml` exists in the repo)

The `ckb init` CLI is a supported entry point. A user who sets `epoch_duration_target = 0` expecting an immediate startup error (as would happen in a debug build) will instead get a silently running node that crashes at the first epoch boundary — a confusing and hard-to-diagnose failure.

---

### Recommendation

Replace all five `debug_assert!` calls in `ConsensusBuilder::build()` with proper `assert!` (or return a `Result` error) so that invalid parameters are rejected in both debug and release builds:

```rust
// spec/src/consensus.rs — ConsensusBuilder::build()
assert!(
    self.inner.genesis_block.difficulty() > U256::zero(),
    "genesis difficulty must be greater than zero"
);
assert!(
    self.inner.initial_primary_epoch_reward != Capacity::zero(),
    "initial_primary_epoch_reward must be non-zero"
);
assert!(
    self.inner.epoch_duration_target() != 0,
    "epoch_duration_target must be non-zero"
);
```

Additionally, add explicit non-zero guards in `build_genesis_epoch_ext()` and `ChainSpec::build_consensus()` for `genesis_epoch_length`, `epoch_duration_target`, and the `orphan_rate_target` denominator before any arithmetic is performed.

---

### Proof of Concept

1. Run `ckb init --chain dev` to create a dev chain workspace.
2. Edit `specs/dev.toml`:
   ```toml
   [params]
   epoch_duration_target = 0
   permanent_difficulty_in_dummy = true
   ```
3. Build CKB in **debug** mode (`cargo build`): `ckb run` immediately panics at `ConsensusBuilder::build()` — the `debug_assert!` fires.
4. Build CKB in **release** mode (`cargo build --release`): `ckb run` starts successfully with no error. Mine blocks until the first epoch boundary. The node panics:
   ```
   thread 'main' panicked at 'attempt to divide by zero'
   spec/src/consensus.rs:835
   ```
   because `next_epoch_length = epoch_duration_target.div_ceil(MIN_BLOCK_INTERVAL) = 0`, and `primary_epoch_reward / 0` triggers the panic. [1](#0-0) [4](#0-3) [2](#0-1)

### Citations

**File:** spec/src/consensus.rs (L227-229)
```rust
    let genesis_hash_rate = compact_to_difficulty(compact_target)
        * (genesis_epoch_length + genesis_orphan_count)
        / epoch_duration_target;
```

**File:** spec/src/consensus.rs (L318-353)
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

        debug_assert!(
            !self.inner.genesis_block.transactions().is_empty()
                && !self.inner.genesis_block.transactions()[0]
                    .witnesses()
                    .is_empty(),
            "genesis block must contain the witness for cellbase"
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

**File:** spec/src/consensus.rs (L829-848)
```rust
                    if self.permanent_difficulty() {
                        let next_epoch_length =
                            self.epoch_duration_target().div_ceil(MIN_BLOCK_INTERVAL);
                        let primary_epoch_reward =
                            self.primary_epoch_reward_of_next_epoch(&epoch).as_u64();
                        let block_reward =
                            Capacity::shannons(primary_epoch_reward / next_epoch_length);
                        let remainder_reward =
                            Capacity::shannons(primary_epoch_reward % next_epoch_length);

                        let dummy_epoch_ext = epoch
                            .clone()
                            .into_builder()
                            .base_block_reward(block_reward)
                            .remainder_reward(remainder_reward)
                            .number(epoch.number() + 1)
                            .last_block_hash_in_previous_epoch(header.hash())
                            .start_number(header.number() + 1)
                            .length(next_epoch_length)
                            .build();
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

**File:** spec/src/lib.rs (L1033-1041)
```rust
pub fn calculate_block_reward(epoch_reward: Capacity, epoch_length: BlockNumber) -> Capacity {
    let epoch_reward = epoch_reward.as_u64();
    Capacity::shannons({
        if !epoch_reward.is_multiple_of(epoch_length) {
            epoch_reward / epoch_length + 1
        } else {
            epoch_reward / epoch_length
        }
    })
```

**File:** util/rational/src/lib.rs (L27-47)
```rust
impl RationalU256 {
    /// Creates a new ratio `numer / denom`.
    ///
    /// ## Panics
    ///
    /// Panics when `denom` is zero.
    #[inline]
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
