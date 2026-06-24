Audit Report

## Title
`CKB2021_START_EPOCH` Set to `0` Instead of Historical Activation Epoch Causes Consensus Divergence — (File: `util/constant/src/hardfork/mainnet.rs`, `util/constant/src/hardfork/testnet.rs`)

## Summary
In both `util/constant/src/hardfork/mainnet.rs` and `util/constant/src/hardfork/testnet.rs`, the active `CKB2021_START_EPOCH` constant is `0`, while the historically correct values (`5414` for mainnet, `3113` for testnet) are commented out. This causes every node built from this codebase to apply CKB2021 consensus rules (RFC 0030, RFC 0038, and others) from genesis, making it reject historical blocks that were valid under pre-2021 rules and preventing it from syncing the canonical mainnet or testnet chain.

## Finding Description
**Root cause:** `util/constant/src/hardfork/mainnet.rs` line 8 sets `pub const CKB2021_START_EPOCH: u64 = 0`, with the correct value `5414` commented out on line 7. The same pattern exists in `util/constant/src/hardfork/testnet.rs` line 8 (`0` active, `3113` commented out on line 7).

**Code path 1 — `CKB2021::new_mirana()`** in `util/types/src/core/hardfork/ckb2021.rs` lines 91–96 passes `hardfork::mainnet::CKB2021_START_EPOCH` (= `0`) as the activation epoch for RFC 0029, 0030, 0031, 0036, and 0038. This is consumed by `ConsensusBuilder::new()` in `spec/src/consensus.rs` line 298 via `hardfork_switch: HardForks::new_mirana()`, making it the default for every mainnet consensus instance.

**Code path 2 — `HardForkConfig::complete_mainnet()`** in `spec/src/hardfork.rs` lines 23–27 also passes `mainnet::CKB2021_START_EPOCH` (= `0`) to `update_2021`, which sets RFC 0029, 0030, 0031, 0036, 0038 activation epochs to `0`.

Both code paths are affected. RFC 0030 (`check_length_in_epoch_since`) and RFC 0038 (`disallow_over_max_dep_expansion_limit`) are **stricter** rules. Historical mainnet blocks in epochs 0–5413 were produced and validated under pre-2021 rules; any such block containing a transaction with `since` epoch index ≥ length (valid pre-2021, rejected by RFC 0030) or exceeding the dep expansion limit (valid pre-2021, rejected by RFC 0038) will be rejected by this node. No runtime guard, assertion, or test cross-checks `CKB2021_START_EPOCH` against the known historical activation epoch.

## Impact Explanation
This is a **Critical** impact: **Vulnerabilities which could easily cause consensus deviation**. A node running this code cannot sync the canonical mainnet chain from genesis because it applies stricter post-2021 validation rules to historical pre-2021 blocks, causing it to reject blocks that the rest of the network accepted. The node diverges from mainnet consensus immediately upon encountering any historical block that exercises the affected validation paths.

## Likelihood Explanation
Any operator who builds and runs this node against mainnet or testnet will trigger the divergence during initial chain sync. No special attacker capability is required — the divergence is deterministic and reproducible by any node operator. The root cause is a production constant file with the correct value commented out and replaced with `0`, with no compensating guard.

## Recommendation
Restore the correct activation epoch values:

```rust
// util/constant/src/hardfork/mainnet.rs
pub const CKB2021_START_EPOCH: u64 = 5414;

// util/constant/src/hardfork/testnet.rs
pub const CKB2021_START_EPOCH: u64 = 3113;
```

Add compile-time or test-time assertions that cross-check `CKB2021_START_EPOCH` against `RFC0028_RFC0032_RFC0033_RFC0034_START_EPOCH` for mainnet (they must be equal per the historical activation record), and analogous checks for testnet.

## Proof of Concept
1. `util/constant/src/hardfork/mainnet.rs` line 8: active `CKB2021_START_EPOCH = 0`; correct value `5414` commented out on line 7. [1](#0-0) 
2. `util/constant/src/hardfork/testnet.rs` line 8: active `CKB2021_START_EPOCH = 0`; correct value `3113` commented out on line 7. [2](#0-1) 
3. `CKB2021::new_mirana()` passes `CKB2021_START_EPOCH` (= `0`) for RFC 0029, 0030, 0031, 0036, 0038. [3](#0-2) 
4. `ConsensusBuilder::new()` sets `hardfork_switch: HardForks::new_mirana()`, propagating the wrong epoch into every mainnet consensus instance. [4](#0-3) 
5. `HardForkConfig::complete_mainnet()` also passes `mainnet::CKB2021_START_EPOCH` (= `0`) to `update_2021`. [5](#0-4) 
6. To reproduce: build the node, connect to mainnet, and observe sync failure when the node reaches any historical block (epochs 0–5413) containing a transaction with `since` epoch index ≥ length or exceeding the dep expansion limit — both valid under pre-2021 rules but rejected under RFC 0030/0038 activated at epoch 0.

### Citations

**File:** util/constant/src/hardfork/mainnet.rs (L6-8)
```rust
/// First epoch number for CKB v2021, at about 2022/05/10 1:00 UTC
// pub const CKB2021_START_EPOCH: u64 = 5414;
pub const CKB2021_START_EPOCH: u64 = 0;
```

**File:** util/constant/src/hardfork/testnet.rs (L6-8)
```rust
/// First epoch number for CKB v2021, at about 2021/10/24 3:15 UTC.
// pub const CKB2021_START_EPOCH: u64 = 3113;
pub const CKB2021_START_EPOCH: u64 = 0;
```

**File:** util/types/src/core/hardfork/ckb2021.rs (L91-96)
```rust
            .rfc_0029(hardfork::mainnet::CKB2021_START_EPOCH)
            .rfc_0030(hardfork::mainnet::CKB2021_START_EPOCH)
            .rfc_0031(hardfork::mainnet::CKB2021_START_EPOCH)
            .rfc_0032(hardfork::mainnet::RFC0028_RFC0032_RFC0033_RFC0034_START_EPOCH)
            .rfc_0036(hardfork::mainnet::CKB2021_START_EPOCH)
            .rfc_0038(hardfork::mainnet::CKB2021_START_EPOCH)
```

**File:** spec/src/consensus.rs (L298-298)
```rust
                hardfork_switch: HardForks::new_mirana(),
```

**File:** spec/src/hardfork.rs (L22-27)
```rust
        let mut ckb2021 = CKB2021::new_builder();
        ckb2021 = self.update_2021(
            ckb2021,
            mainnet::CKB2021_START_EPOCH,
            mainnet::RFC0028_RFC0032_RFC0033_RFC0034_START_EPOCH,
        )?;
```
