### Title
Missing Zero-Value Validation on Consensus Initializer Parameters Causes Node Panic in Release Builds — (`File: spec/src/consensus.rs`)

---

### Summary

`build_genesis_epoch_ext` performs integer division by `genesis_epoch_length`, `genesis_orphan_rate.1`, and `epoch_duration_target` without any zero-checks. The `ConsensusBuilder::orphan_rate_target` setter stores a `RationalU256` with a potentially zero denominator via `new_raw`, bypassing the safe constructor. The only guards in `ConsensusBuilder::build()` are `debug_assert!` macros, which are **completely elided in release builds**. A supported local CLI user who configures a custom chain spec with any of these values set to zero will cause an unrecoverable node panic.

---

### Finding Description

**Root cause 1 — `build_genesis_epoch_ext` performs unchecked integer division:** [1](#0-0) 

Line 222 divides `epoch_reward.as_u64() / genesis_epoch_length`. Line 226 divides by `genesis_orphan_rate.1 as u64`. Line 229 divides by `epoch_duration_target`. All three are bare Rust integer divisions — a zero divisor causes an immediate `panic` in both debug and release builds.

**Root cause 2 — `ConsensusBuilder::orphan_rate_target` setter uses `new_raw`, bypassing the zero-denominator guard:** [2](#0-1) 

`RationalU256::new` panics on a zero denominator (documented), but `new_raw` skips that check entirely: [3](#0-2) 

If `orphan_rate_target = (x, 0)` is written into `Consensus.orphan_rate_target`, subsequent arithmetic in `next_epoch_ext` — specifically `orphan_rate_target + U256::one()` — will call `RationalU256::new` internally and panic during block processing. [4](#0-3) 

**Root cause 3 — `ConsensusBuilder::build()` guards are `debug_assert!` only:** [5](#0-4) 

`debug_assert!` is a no-op in release builds (`--release`). There is no `assert!` or `Result`-returning validation for `genesis_epoch_length`, `orphan_rate_target` denominator, `initial_primary_epoch_reward`, or `epoch_duration_target`. The `Params` struct accepts all of these as plain `Option<u64>` / `Option<(u32,u32)>` from TOML with no range enforcement: [6](#0-5) 

`build_consensus` passes them directly into `build_genesis_epoch_ext` and `ConsensusBuilder` setters without any intermediate validation: [7](#0-6) 

---

### Impact Explanation

- `genesis_epoch_length = 0`: node panics at startup inside `build_genesis_epoch_ext` (line 222, division by zero). Node cannot start.
- `epoch_duration_target = 0`: node panics at startup inside `build_genesis_epoch_ext` (line 229, division by zero). Node cannot start.
- `orphan_rate_target = [x, 0]`: node panics at startup inside `build_genesis_epoch_ext` (line 226, division by zero). Additionally, if the value somehow reaches `Consensus.orphan_rate_target` with a zero denominator, the node panics during the first epoch transition when `next_epoch_ext` performs arithmetic on it.

In all cases the process terminates with an unhandled panic. In a release build there is no safety net because `debug_assert!` is compiled out.

---

### Likelihood Explanation

The `[params]` section of a chain spec TOML is user-editable and all three fields are optional with no documented minimum. The dev spec already sets `genesis_epoch_length = 10` and `epoch_duration_target = 80`; a developer experimenting with extreme values (e.g., `genesis_epoch_length = 0` to disable issuance, or `orphan_rate_target = [0, 0]`) would trigger the panic immediately. Because the only guards are `debug_assert!`, a release-mode binary provides no error message — just a panic.

---

### Recommendation

1. Replace all three `debug_assert!` guards in `ConsensusBuilder::build()` with hard `assert!` or, better, return a `Result<Consensus, Error>` so callers can handle misconfiguration gracefully.
2. In `build_genesis_epoch_ext`, add explicit zero-checks for `genesis_epoch_length`, `epoch_duration_target`, and `genesis_orphan_rate.1` before performing division, returning an error or panicking with a clear message.
3. In `ConsensusBuilder::orphan_rate_target`, replace `RationalU256::new_raw` with `RationalU256::new` (which already panics on zero denominator with a clear message), or validate the denominator before storing.
4. In `Params` accessors or `build_consensus`, validate that `genesis_epoch_length > 0`, `epoch_duration_target > 0`, and `orphan_rate_target.1 > 0` before passing them downstream, returning a `Box<dyn Error>` consistent with the existing `build_consensus` signature.

---

### Proof of Concept

Configure a dev chain spec with:
```toml
[params]
genesis_epoch_length = 0
epoch_duration_target = 14400
```

Run in release mode:
```
cargo build --release
./target/release/ckb init --chain dev
# edit ckb.toml to point to the modified spec
./target/release/ckb run
```

The node panics immediately at startup with `attempt to divide by zero` inside `build_genesis_epoch_ext` at `spec/src/consensus.rs:222`. No error is caught or logged — the process exits with a non-zero status. The same panic is reproducible with `epoch_duration_target = 0` (line 229) or `orphan_rate_target = [1, 0]` (line 226). In a debug build, the `debug_assert!` at line 342–345 fires first for `epoch_duration_target`, but in a release build it is silently skipped, and the division-by-zero in `build_genesis_epoch_ext` is the first point of failure. [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** spec/src/consensus.rs (L214-241)
```rust
/// Build the epoch information of genesis block
pub fn build_genesis_epoch_ext(
    epoch_reward: Capacity,
    compact_target: u32,
    genesis_epoch_length: BlockNumber,
    epoch_duration_target: u64,
    genesis_orphan_rate: (u32, u32),
) -> EpochExt {
    let block_reward = Capacity::shannons(epoch_reward.as_u64() / genesis_epoch_length);
    let remainder_reward = Capacity::shannons(epoch_reward.as_u64() % genesis_epoch_length);

    let genesis_orphan_count =
        genesis_epoch_length * genesis_orphan_rate.0 as u64 / genesis_orphan_rate.1 as u64;
    let genesis_hash_rate = compact_to_difficulty(compact_target)
        * (genesis_epoch_length + genesis_orphan_count)
        / epoch_duration_target;

    EpochExt::new_builder()
        .number(0)
        .base_block_reward(block_reward)
        .remainder_reward(remainder_reward)
        .previous_epoch_hash_rate(genesis_hash_rate)
        .last_block_hash_in_previous_epoch(Byte32::zero())
        .start_number(0)
        .length(genesis_epoch_length)
        .compact_target(compact_target)
        .build()
}
```

**File:** spec/src/consensus.rs (L317-365)
```rust
    /// Build a new Consensus by taking ownership of the `Builder`, and returns a [`Consensus`].
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

        self.inner.dao_type_hash = self.get_type_hash(OUTPUT_INDEX_DAO).unwrap_or_default();
        self.inner.secp256k1_blake160_sighash_all_type_hash =
            self.get_type_hash(OUTPUT_INDEX_SECP256K1_BLAKE160_SIGHASH_ALL);
        self.inner.secp256k1_blake160_multisig_all_type_hash =
            self.get_type_hash(OUTPUT_INDEX_SECP256K1_BLAKE160_MULTISIG_ALL);
        self.inner
            .genesis_epoch_ext
            .set_compact_target(self.inner.genesis_block.compact_target());
        self.inner.genesis_hash = self.inner.genesis_block.hash();
        self.inner
    }
```

**File:** spec/src/consensus.rs (L386-393)
```rust
    /// Sets orphan_rate_target for the new Consensus.
    pub fn orphan_rate_target(mut self, orphan_rate_target: (u32, u32)) -> Self {
        self.inner.orphan_rate_target = RationalU256::new_raw(
            U256::from(orphan_rate_target.0),
            U256::from(orphan_rate_target.1),
        );
        self
    }
```

**File:** spec/src/consensus.rs (L871-894)
```rust
                        let orphan_rate_target = self.orphan_rate_target();
                        let epoch_duration_target = self.epoch_duration_target();
                        let epoch_duration_target_u256 = U256::from(self.epoch_duration_target());
                        let last_epoch_length_u256 = U256::from(epoch.length());
                        let last_orphan_rate = RationalU256::new(
                            U256::from(epoch_uncles_count),
                            last_epoch_length_u256.clone(),
                        );

                        let (next_epoch_length, bound) = if epoch_uncles_count == 0 {
                            (
                                cmp::min(self.max_epoch_length(), epoch.length() * TAU),
                                true,
                            )
                        } else {
                            // o_ideal * (1 + o_i ) * L_ideal * C_i,m
                            let numerator = orphan_rate_target
                                * (&last_orphan_rate + U256::one())
                                * &epoch_duration_target_u256
                                * &last_epoch_length_u256;
                            // o_i * (1 + o_ideal ) * L_i
                            let denominator = &last_orphan_rate
                                * (orphan_rate_target + U256::one())
                                * &last_epoch_duration;
```

**File:** util/rational/src/lib.rs (L34-41)
```rust
    pub fn new(numer: U256, denom: U256) -> RationalU256 {
        if denom.is_zero() {
            panic!("denominator == 0");
        }
        let mut ret = RationalU256::new_raw(numer, denom);
        ret.reduce();
        ret
    }
```

**File:** spec/src/lib.rs (L219-241)
```rust
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
```

**File:** spec/src/lib.rs (L562-594)
```rust
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
```
