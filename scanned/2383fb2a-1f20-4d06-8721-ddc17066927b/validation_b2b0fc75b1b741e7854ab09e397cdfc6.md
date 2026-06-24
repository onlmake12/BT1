Audit Report

## Title
Missing Zero-Value Validation on Consensus Parameters Causes Unhandled Panic at Node Startup — (`File: spec/src/consensus.rs`)

## Summary
`build_genesis_epoch_ext` performs bare integer division by `genesis_epoch_length`, `genesis_orphan_rate.1`, and `epoch_duration_target` with no zero-checks. The only guards in `ConsensusBuilder::build()` are `debug_assert!` macros, which are silently elided in release builds. A local user who configures a custom chain spec with any of these values set to zero will cause an unrecoverable panic at node startup, bypassing the `Result`-returning error path that `build_consensus` is designed to use.

## Finding Description
**Root cause 1 — unchecked division in `build_genesis_epoch_ext`:**
Lines 222, 226, and 229 of `spec/src/consensus.rs` perform bare Rust integer division by `genesis_epoch_length`, `genesis_orphan_rate.1 as u64`, and `epoch_duration_target` respectively. Rust integer division by zero panics unconditionally in both debug and release builds. No guard precedes any of these operations.

**Root cause 2 — `ConsensusBuilder::orphan_rate_target` uses `new_raw`:**
Lines 387–392 of `spec/src/consensus.rs` store the orphan rate via `RationalU256::new_raw`, which explicitly skips the zero-denominator check present in `RationalU256::new` (lines 34–41 of `util/rational/src/lib.rs`). A zero denominator stored here would also cause a panic during the first epoch transition in `next_epoch_ext` when arithmetic is performed on `orphan_rate_target`.

**Root cause 3 — `debug_assert!` guards are no-ops in release builds:**
Lines 319–353 of `spec/src/consensus.rs` contain four `debug_assert!` blocks covering genesis difficulty, witness presence, `initial_primary_epoch_reward`, and `epoch_duration_target`. All are compiled out with `--release`. There are no `assert!` or `Result`-returning equivalents.

**Root cause 4 — `Params` struct and `build_consensus` perform no range validation:**
`Params` (lines 185–250 of `spec/src/lib.rs`) accepts `genesis_epoch_length`, `epoch_duration_target`, and `orphan_rate_target` as plain `Option<T>` from TOML with no minimum enforcement. `build_consensus` (lines 560–600 of `spec/src/lib.rs`) passes them directly into `build_genesis_epoch_ext` and `ConsensusBuilder` setters. The function signature returns `Result<Consensus, Box<dyn Error>>`, but the panic in `build_genesis_epoch_ext` is never caught — it unwinds the process before the `Result` can be returned.

## Impact Explanation
The concrete impact is a local command line crash: the node process terminates with an unhandled panic at startup when a custom chain spec contains a zero value for any of the three parameters. No error is logged or returned; the process exits with a non-zero status. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local command line crash**. The claimed "High" severity is not supported because the trigger requires local operator misconfiguration and cannot be exercised by a remote or unprivileged network peer.

## Likelihood Explanation
The `[params]` section of a chain spec TOML is fully user-editable. All three fields are optional with no documented minimum value. A developer experimenting with a custom dev chain (e.g., setting `genesis_epoch_length = 0` to test issuance behavior, or `orphan_rate_target = [0, 0]`) would trigger the panic immediately on the first `ckb run` invocation in release mode. The absence of any error message makes diagnosis non-obvious.

## Recommendation
1. Replace the four `debug_assert!` blocks in `ConsensusBuilder::build()` with hard `assert!` calls, or change `build()` to return `Result<Consensus, Error>` so callers can handle misconfiguration gracefully.
2. In `build_genesis_epoch_ext`, add explicit zero-checks for `genesis_epoch_length`, `epoch_duration_target`, and `genesis_orphan_rate.1` before division, returning an error or panicking with a descriptive message.
3. In `ConsensusBuilder::orphan_rate_target`, replace `RationalU256::new_raw` with `RationalU256::new`, which already panics with a clear message on a zero denominator.
4. In `build_consensus` or `Params` accessors, validate `genesis_epoch_length > 0`, `epoch_duration_target > 0`, and `orphan_rate_target.1 > 0` before passing them downstream, returning a `Box<dyn Error>` consistent with the existing `build_consensus` signature.

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
The process panics immediately at `spec/src/consensus.rs:222` (`attempt to divide by zero`). The same panic is reproducible with `epoch_duration_target = 0` (line 229) or `orphan_rate_target = [1, 0]` (line 226). In a debug build the `debug_assert!` at line 342 fires first for `epoch_duration_target`, but in a release build it is silently skipped.