Audit Report

## Title
Unchecked Zero-Value Division in `build_genesis_epoch_ext` Causes Node Panic on Custom Chain Spec — (`File: spec/src/consensus.rs`)

## Summary
`build_genesis_epoch_ext` performs bare integer division by `genesis_epoch_length`, `genesis_orphan_rate.1`, and `epoch_duration_target` with no zero-checks. The only guards in `ConsensusBuilder::build()` are `debug_assert!` macros, which are compiled out in release builds. A local user running a release-mode binary with a custom chain spec containing any of these values set to zero will trigger an unrecoverable panic at node startup.

## Finding Description
**Root cause 1 — Unchecked division in `build_genesis_epoch_ext`:**

Line 222 divides `epoch_reward.as_u64() / genesis_epoch_length` and line 223 uses `% genesis_epoch_length`. Line 226 divides by `genesis_orphan_rate.1 as u64`. Line 229 divides by `epoch_duration_target`. All three are bare Rust integer divisions with no prior zero-check. A zero divisor causes an immediate `panic` in both debug and release builds. [1](#0-0) 

**Root cause 2 — `ConsensusBuilder::orphan_rate_target` uses `new_raw`, bypassing the zero-denominator guard:**

`RationalU256::new` panics on a zero denominator (documented at `util/rational/src/lib.rs:34-37`), but `new_raw` skips that check entirely. If `orphan_rate_target = (x, 0)` is stored, subsequent arithmetic in `next_epoch_ext` that calls `RationalU256::new` internally will panic during block processing. [2](#0-1) [3](#0-2) 

**Root cause 3 — `ConsensusBuilder::build()` guards are `debug_assert!` only:**

All four guards in `build()` use `debug_assert!`, which is a no-op in release builds. There is no `assert!` or `Result`-returning validation for `genesis_epoch_length`, `orphan_rate_target` denominator, `initial_primary_epoch_reward`, or `epoch_duration_target`. [4](#0-3) 

**Root cause 4 — `Params` and `build_consensus` pass values through without validation:**

`Params` accepts `genesis_epoch_length` as `Option<BlockNumber>` and `orphan_rate_target` as `Option<(u32, u32)>` from TOML with no range enforcement. `build_consensus` passes them directly into `build_genesis_epoch_ext` and `ConsensusBuilder` setters without any intermediate validation. [5](#0-4) [6](#0-5) 

## Impact Explanation
This matches **Note (0–500 points): Any local command line crash**. A user running `ckb run` in release mode with a custom chain spec containing `genesis_epoch_length = 0`, `epoch_duration_target = 0`, or `orphan_rate_target = [x, 0]` will get an unhandled panic with no useful error message. The process exits with a non-zero status and no recovery path. The impact is strictly local — it does not affect the broader CKB network.

## Likelihood Explanation
The `[params]` section of a chain spec TOML is user-editable and all three fields are optional with no documented minimum. A developer experimenting with a custom dev chain (e.g., setting `genesis_epoch_length = 0` to test issuance behavior, or `orphan_rate_target = [0, 0]`) would trigger the panic immediately on `ckb run`. Because the only guards are `debug_assert!`, a release-mode binary provides no error message — just a panic. This is realistically triggerable by any local CLI user working with custom chain specs.

## Recommendation
1. Replace all `debug_assert!` guards in `ConsensusBuilder::build()` with hard `assert!` or return a `Result<Consensus, Error>` for graceful error handling.
2. In `build_genesis_epoch_ext`, add explicit zero-checks for `genesis_epoch_length`, `epoch_duration_target`, and `genesis_orphan_rate.1` before performing division, returning an error or panicking with a clear diagnostic message.
3. In `ConsensusBuilder::orphan_rate_target`, replace `RationalU256::new_raw` with `RationalU256::new` (which already panics on zero denominator with a clear message), or validate the denominator before storing.
4. In `Params` accessors or `build_consensus`, validate that `genesis_epoch_length > 0`, `epoch_duration_target > 0`, and `orphan_rate_target.1 > 0` before passing them downstream, returning a `Box<dyn Error>` consistent with the existing `build_consensus` signature.

## Proof of Concept
Configure a dev chain spec with:
```toml
[params]
genesis_epoch_length = 0
epoch_duration_target = 14400
```
Build and run in release mode:
```
cargo build --release
./target/release/ckb init --chain dev
# edit spec to set genesis_epoch_length = 0
./target/release/ckb run
```
The node panics immediately at startup with `attempt to divide by zero` inside `build_genesis_epoch_ext` at `spec/src/consensus.rs:222`. No error is caught or logged — the process exits with a non-zero status. The same panic is reproducible with `epoch_duration_target = 0` (line 229) or `orphan_rate_target = [1, 0]` (line 226). In a debug build, the `debug_assert!` at lines 342–345 fires first for `epoch_duration_target`, but in a release build it is silently skipped.

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

**File:** spec/src/consensus.rs (L319-353)
```rust
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
