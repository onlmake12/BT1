All three code claims are confirmed against the actual source. The divisions at L222-229 of `spec/src/consensus.rs` are unguarded, the `debug_assert!` guards at L337-345 are release-stripped, `ConsensusBuilder::orphan_rate_target` uses `new_raw` at L387-393, and `build_consensus` calls `build_genesis_epoch_ext` with no pre-validation at L562-568. The crash is real and reproducible. The impact, however, is confined to a node operator crashing their own node via a self-authored chain-spec TOML — this maps to the "local command line crash" Note tier, not the High "crash a CKB node" tier (which requires an external attacker path).

---

Audit Report

## Title
Unguarded Zero-Denominator Integer Divisions in `build_genesis_epoch_ext` Cause Startup Panic on Misconfigured Chain Spec — (File: `spec/src/consensus.rs`)

## Summary
`build_genesis_epoch_ext` in `spec/src/consensus.rs` performs three integer divisions using `genesis_epoch_length`, `genesis_orphan_rate.1`, and `epoch_duration_target` with no zero-checks before use. `ConsensusBuilder::build()` guards only two of these with `debug_assert!`, which is a no-op in every release binary, and the `orphan_rate_target` denominator has no guard at all. A node operator who sets any of these parameters to zero in their chain-spec TOML triggers an unrecoverable division-by-zero panic at `ckb run` startup with no diagnostic message.

## Finding Description
`build_genesis_epoch_ext` (L222–229, `spec/src/consensus.rs`) performs:

```rust
let block_reward = Capacity::shannons(epoch_reward.as_u64() / genesis_epoch_length); // panics if 0
let remainder_reward = Capacity::shannons(epoch_reward.as_u64() % genesis_epoch_length);
let genesis_orphan_count =
    genesis_epoch_length * genesis_orphan_rate.0 as u64 / genesis_orphan_rate.1 as u64; // panics if denom 0
let genesis_hash_rate = compact_to_difficulty(compact_target)
    * (genesis_epoch_length + genesis_orphan_count)
    / epoch_duration_target; // panics if 0
```

All three divisors come from the `Params` struct, which is deserialized from a user-editable TOML with no range enforcement. `ConsensusBuilder::build()` (L337–345) guards `epoch_duration_target` and `initial_primary_epoch_reward` with `debug_assert!`, which compiles to nothing in `--release` builds. The `orphan_rate_target` denominator has no guard whatsoever. `ConsensusBuilder::orphan_rate_target` (L387–393) uses `RationalU256::new_raw`, bypassing the zero-check in `RationalU256::new`. `ChainSpec::build_consensus` (L560–568, `spec/src/lib.rs`) calls `build_genesis_epoch_ext` unconditionally before any validation, so the panic fires at node startup.

## Impact Explanation
The impact is a **local command line crash (Note, 0–500 points)**. The crash requires the node operator to author or deploy a chain-spec TOML with a zero value for one of these parameters and then run `ckb run` against it. Mainnet and testnet specs use bundled safe defaults and are unaffected. The crash cannot be triggered remotely or by an unprivileged external user against a correctly configured node; it is entirely self-inflicted by the operator of a custom or dev network. This maps to the "Any local command line crash" Note-tier impact, not the High-tier "crash a CKB node" impact (which requires an external attacker path against a running node).

## Likelihood Explanation
Only a node operator configuring their own chain spec can trigger this. The mainnet and testnet are unaffected. The scenario is limited to custom or dev networks where an operator accidentally or via a misconfigured deployment script sets one of these parameters to zero. Likelihood is low.

## Recommendation
1. Replace `debug_assert!` guards in `ConsensusBuilder::build()` with `assert!` or convert `build()` to return `Result<Consensus, Error>` so invalid configurations are caught in release builds with a clear error message.
2. Add explicit lower-bound checks (`> 0`) for `genesis_epoch_length`, `epoch_duration_target`, and `orphan_rate_target.1` inside `ChainSpec::build_consensus()` before calling `build_genesis_epoch_ext`, returning a descriptive `Err` instead of panicking.
3. Replace `RationalU256::new_raw` with `RationalU256::new` in `ConsensusBuilder::orphan_rate_target` to enforce the zero-denominator guard at the point of construction.
4. Add range validation to the `Params` deserialization layer so a malformed TOML produces a descriptive parse error rather than a deferred runtime panic.

## Proof of Concept
Add to any custom chain spec `[params]`:
```toml
[params]
orphan_rate_target = [1, 0]
```
Run `ckb run`. `ChainSpec::build_consensus()` calls `build_genesis_epoch_ext(..., (1, 0))`. At L226:
```rust
genesis_epoch_length * 1u64 / 0u64  // attempt to divide by zero → panic
```
The node crashes immediately with `attempt to divide by zero`. The same crash is triggered by `genesis_epoch_length = 0` (L222) or `epoch_duration_target = 0` (L229). In release builds the `debug_assert!` at L342–345 that would have caught `epoch_duration_target = 0` is silently absent.