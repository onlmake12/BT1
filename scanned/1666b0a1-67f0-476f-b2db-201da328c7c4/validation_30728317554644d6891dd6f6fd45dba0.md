### Title
`debug_assert!`-Only Validation in `ConsensusBuilder::build()` Leaves Critical Consensus Parameters Unvalidated in Release Builds — (File: `spec/src/consensus.rs`)

### Summary

`ConsensusBuilder::build()` uses Rust's `debug_assert!` macro — which is compiled out entirely in release builds — as the sole guard for critical consensus invariants such as `epoch_duration_target != 0` and `initial_primary_epoch_reward != 0`. The downstream computation function `build_genesis_epoch_ext()` performs integer divisions using these parameters without any independent validation. In a production release binary, a misconfigured chain spec with a zero `epoch_duration_target` or `genesis_epoch_length` passes through the builder silently and causes a panic (integer division by zero) at node startup or at the first epoch boundary. This is a direct structural analog to the Atlendis finding: validation logic lives in the "factory" (`ConsensusBuilder::build()`), not in the functions that actually consume the values (`build_genesis_epoch_ext`, `next_epoch_ext`).

---

### Finding Description

**Root cause — validation only in the builder, disabled in release:**

`ConsensusBuilder::build()` contains five `debug_assert!` guards:

```rust
// spec/src/consensus.rs  ~line 318-345
pub fn build(mut self) -> Consensus {
    debug_assert!(
        self.inner.genesis_block.difficulty() > U256::zero(),
        "genesis difficulty should greater than zero"
    );
    debug_assert!(
        self.inner.initial_primary_epoch_reward != Capacity::zero(),
        "initial_primary_epoch_reward must be non-zero"
    );
    debug_assert!(
        self.inner.epoch_duration_target() != 0,
        "epoch_duration_target must be non-zero"
    );
    // ...
    self.inner
}
``` [1](#0-0) 

In Rust, `debug_assert!` expands to nothing when compiled with `--release`. Every production CKB binary is built with `--release`. The three guards above are therefore completely absent at runtime in any deployed node.

**Computation without validation — `build_genesis_epoch_ext`:**

The function that actually constructs the genesis epoch data performs bare integer divisions:

```rust
// spec/src/consensus.rs  ~line 222-229
let block_reward   = Capacity::shannons(epoch_reward.as_u64() / genesis_epoch_length);
let remainder_reward = Capacity::shannons(epoch_reward.as_u64() % genesis_epoch_length);
let genesis_orphan_count =
    genesis_epoch_length * genesis_orphan_rate.0 as u64 / genesis_orphan_rate.1 as u64;
let genesis_hash_rate =
    compact_to_difficulty(compact_target)
        * (genesis_epoch_length + genesis_orphan_count)
        / epoch_duration_target;
``` [2](#0-1) 

Neither `genesis_epoch_length` nor `epoch_duration_target` is checked for zero before these divisions. The only place that is supposed to enforce non-zero is the `debug_assert!` in `build()` — which is a no-op in release.

**Call order makes the builder guard unreachable for `genesis_epoch_length`:**

In `spec/src/lib.rs`, `build_genesis_epoch_ext()` is called *before* `ConsensusBuilder::build()`:

```rust
// spec/src/lib.rs  ~line 562-600
let genesis_epoch_ext = build_genesis_epoch_ext(
    self.params.initial_primary_epoch_reward(),
    self.genesis.compact_target,
    self.params.genesis_epoch_length(),   // ← divided by, unchecked
    self.params.epoch_duration_target(),  // ← divided by, unchecked
    self.params.orphan_rate_target(),
);
// ...
Ok(builder.build())   // debug_assert! here is too late
``` [3](#0-2) 

For `genesis_epoch_length = 0`, the panic occurs inside `build_genesis_epoch_ext` before `build()` is ever reached, so the `debug_assert!` guard is structurally unreachable for that parameter. For `epoch_duration_target`, the `debug_assert!` in `build()` would only fire in a debug binary; in release the division in `build_genesis_epoch_ext` panics first.

**Pattern duplication — `next_epoch_ext`:**

The same `epoch_duration_target` is used in epoch-adjustment arithmetic inside `next_epoch_ext` (called on every epoch boundary). If a `Consensus` object were ever constructed with `epoch_duration_target = 0` through any path that bypasses the builder (e.g., a future refactor, a test helper, or a direct `ConsensusBuilder::new(...).build()` call), the node would panic at the first epoch transition rather than at startup. [4](#0-3) 

---

### Impact Explanation

A node operator (or developer) who configures a custom chain spec with `genesis_epoch_length = 0` or `epoch_duration_target = 0` will receive a release binary that:

- **Panics at startup** (during `build_consensus()`) with an integer division-by-zero, making the node completely unlaunchable.
- Or, if the zero value is introduced through a code path that bypasses `build_genesis_epoch_ext` (e.g., a future refactor that constructs `EpochExt` directly), **panics at the first epoch boundary** — a consensus-critical moment — crashing the node mid-operation.

Because `debug_assert!` is silent in release, the operator receives no diagnostic error; the node simply aborts. For a network of nodes sharing a custom chain spec, this could cause a coordinated crash of all participants at epoch transition.

---

### Likelihood Explanation

Mainnet and testnet use hardcoded, correct parameters, so those deployments are not immediately affected. The risk is real for:

1. **Custom / dev-mode chains** — operators who set `genesis_epoch_length` or `epoch_duration_target` to zero in a TOML spec receive no error in a release build.
2. **Future refactors** — any developer who adds a new `Consensus`-construction path and relies on the builder's `debug_assert!` guards for safety will find those guards absent in production, exactly the scenario described in the Atlendis report.
3. **The `debug_assert!` pattern is already duplicated** across five separate checks in `build()`, increasing the surface area for this class of mistake.

---

### Recommendation

**Short term:** Replace every `debug_assert!` in `ConsensusBuilder::build()` with a proper runtime check that returns a `Result<Consensus, ConsensusError>`. Add equivalent non-zero checks at the top of `build_genesis_epoch_ext()` before any division is performed.

**Long term:** Keep validation as close as possible to where the values are consumed. `build_genesis_epoch_ext()` should validate its own inputs (`genesis_epoch_length > 0`, `epoch_duration_target > 0`, `orphan_rate_target.1 > 0`) and return a `Result`, rather than relying on a caller-side guard that may be disabled or bypassed.

---

### Proof of Concept

1. Create a custom chain spec TOML with `genesis_epoch_length = 0`.
2. Build CKB with `cargo build --release`.
3. Run `ckb run` with that spec.
4. The release binary calls `build_consensus()` → `build_genesis_epoch_ext(..., 0, ...)` → integer division by zero → thread panic → node abort.
5. Repeat with a debug build (`cargo build`): the `debug_assert!` in `build()` fires and prints a clear message — demonstrating that the guard exists only in debug mode and is absent in the production binary. [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** spec/src/consensus.rs (L157-212)
```rust
pub struct ConsensusBuilder {
    inner: Consensus,
}

// Dummy consensus, difficulty can not be zero
impl Default for ConsensusBuilder {
    fn default() -> Self {
        let input = CellInput::new_cellbase_input(0);
        // at least issue some shannons to make dao field valid.
        let output = {
            let empty_output = CellOutput::new_builder().build();
            let occupied = empty_output
                .occupied_capacity(Capacity::zero())
                .expect("default occupied");
            empty_output.as_builder().capacity(occupied).build()
        };
        let witness = Script::default().into_witness();
        let cellbase = TransactionBuilder::default()
            .input(input)
            .output(output)
            .output_data(Bytes::new())
            .witness(witness)
            .build();

        let epoch_ext = build_genesis_epoch_ext(
            INITIAL_PRIMARY_EPOCH_REWARD,
            DIFF_TWO,
            GENESIS_EPOCH_LENGTH,
            DEFAULT_EPOCH_DURATION_TARGET,
            DEFAULT_ORPHAN_RATE_TARGET,
        );
        let primary_issuance =
            calculate_block_reward(INITIAL_PRIMARY_EPOCH_REWARD, GENESIS_EPOCH_LENGTH);
        let secondary_issuance =
            calculate_block_reward(DEFAULT_SECONDARY_EPOCH_REWARD, GENESIS_EPOCH_LENGTH);

        let dao = genesis_dao_data_with_satoshi_gift(
            vec![&cellbase],
            &SATOSHI_PUBKEY_HASH,
            SATOSHI_CELL_OCCUPIED_RATIO,
            primary_issuance,
            secondary_issuance,
        )
        .expect("genesis dao data calculation error!");

        let genesis_block = BlockBuilder::default()
            .compact_target(DIFF_TWO)
            .epoch(EpochNumberWithFraction::new_unchecked(0, 0, 0))
            .dao(dao)
            .transaction(cellbase)
            .build();

        ConsensusBuilder::new(genesis_block, epoch_ext)
            .initial_primary_epoch_reward(INITIAL_PRIMARY_EPOCH_REWARD)
    }
}
```

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

**File:** spec/src/consensus.rs (L318-365)
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

**File:** spec/src/lib.rs (L557-601)
```rust
    /// Build consensus instance
    ///
    /// [Consensus](consensus/struct.Consensus.html)
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
