Audit Report

## Title
`CKB2021_START_EPOCH` Set to `0` Instead of Historical Activation Epoch Causes Consensus Divergence — (`util/constant/src/hardfork/mainnet.rs`, `util/constant/src/hardfork/testnet.rs`)

## Summary

`CKB2021_START_EPOCH` is set to `0` in both `util/constant/src/hardfork/mainnet.rs` and `util/constant/src/hardfork/testnet.rs`, with the historically correct values (`5414` and `3113` respectively) commented out. This causes RFC 0030 and RFC 0038 stricter validation rules to be applied from genesis, causing any node built from this code to reject historical mainnet/testnet blocks that were valid under pre-2021 consensus rules, producing a hard consensus split.

## Finding Description

**Root cause:** In `util/constant/src/hardfork/mainnet.rs` line 8, `CKB2021_START_EPOCH = 0` while the correct value `5414` is commented out on line 7. The same pattern exists in `util/constant/src/hardfork/testnet.rs` line 8 (`0` instead of `3113`). [1](#0-0) [2](#0-1) 

**Code path 1 — `CKB2021::new_mirana()`:** In `util/types/src/core/hardfork/ckb2021.rs` lines 91–96, `new_mirana()` passes `CKB2021_START_EPOCH` (= `0`) as the activation epoch for RFC 0029, 0030, 0031, 0036, and 0038. [3](#0-2) 

**Code path 2 — `ConsensusBuilder::new()`:** In `spec/src/consensus.rs` line 298, the default `hardfork_switch` is `HardForks::new_mirana()`, propagating the wrong epoch into every mainnet consensus instance. [4](#0-3) 

**Code path 3 — `HardForkConfig::complete_mainnet()`:** In `spec/src/hardfork.rs` lines 23–27, `mainnet::CKB2021_START_EPOCH` (= `0`) is passed to `update_2021`, confirming both code paths are affected. [5](#0-4) 

**Historical confirmation:** CHANGELOG.md line 663 records the correct mainnet activation epoch as 5414 ("Activate ckb2021 in mainnet since epoch 5414"). [6](#0-5) 

The critical affected rules are:
- **RFC 0030** (`check_length_in_epoch_since`): Stricter — rejects transactions where `since` epoch `index >= length`. Historical blocks in epochs 0–5413 containing such transactions were valid under pre-2021 rules but are now rejected.
- **RFC 0038** (`disallow_over_max_dep_expansion_limit`): Stricter — rejects transactions exceeding the dep expansion limit. Same historical validity issue.

## Impact Explanation

This is a **Critical** severity finding matching the allowed impact: *"Vulnerabilities which could easily cause consensus deviation."*

A node running this code applies RFC 0030 and RFC 0038 stricter validation to all historical blocks from genesis. Any historical mainnet block in epochs 0–5413 containing a transaction with a `since` epoch field where `index >= length`, or exceeding the dep expansion limit (both valid under pre-2021 rules), will be rejected. The node cannot sync the canonical mainnet chain from genesis, producing a hard consensus split. The node is unable to participate correctly in mainnet consensus.

## Likelihood Explanation

Any operator who builds and runs this node against mainnet or testnet will encounter the divergence during initial chain sync, as soon as a historical block exercising the affected validation paths is reached. No special attacker capability is required — the bug is triggered by normal chain sync against the canonical chain. There is no runtime guard, assertion, or test that cross-checks `CKB2021_START_EPOCH` against the known historical activation epoch.

## Recommendation

Restore the correct activation epoch values:

```rust
// util/constant/src/hardfork/mainnet.rs
pub const CKB2021_START_EPOCH: u64 = 5414;

// util/constant/src/hardfork/testnet.rs
pub const CKB2021_START_EPOCH: u64 = 3113;
```

Additionally, add compile-time or test-time assertions verifying that `CKB2021_START_EPOCH` equals `RFC0028_RFC0032_RFC0033_RFC0034_START_EPOCH` for mainnet (both must be `5414` per historical activation), and similarly for testnet (both must be `3113`).

## Proof of Concept

1. Build a CKB node from this codebase targeting mainnet.
2. Start the node and begin syncing from genesis.
3. The node will apply RFC 0030 (`check_length_in_epoch_since`) and RFC 0038 (`disallow_over_max_dep_expansion_limit`) from epoch 0.
4. Upon reaching any historical mainnet block in epochs 0–5413 containing a transaction with `since` epoch `index >= length` or exceeding the dep expansion limit, the node will reject the block.
5. The node diverges from the canonical chain and cannot complete sync.

A targeted unit test can confirm this by constructing a `CKB2021` instance via `new_mirana()` and asserting `rfc_0030() == 0` and `rfc_0038() == 0` — both will pass against the current (broken) code, demonstrating the wrong activation epoch is in effect.

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

**File:** util/types/src/core/hardfork/ckb2021.rs (L87-99)
```rust
    pub fn new_mirana() -> Self {
        // Use a builder to ensure all features are set manually.
        Self::new_builder()
            .rfc_0028(hardfork::mainnet::RFC0028_RFC0032_RFC0033_RFC0034_START_EPOCH)
            .rfc_0029(hardfork::mainnet::CKB2021_START_EPOCH)
            .rfc_0030(hardfork::mainnet::CKB2021_START_EPOCH)
            .rfc_0031(hardfork::mainnet::CKB2021_START_EPOCH)
            .rfc_0032(hardfork::mainnet::RFC0028_RFC0032_RFC0033_RFC0034_START_EPOCH)
            .rfc_0036(hardfork::mainnet::CKB2021_START_EPOCH)
            .rfc_0038(hardfork::mainnet::CKB2021_START_EPOCH)
            .build()
            .unwrap()
    }
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

**File:** CHANGELOG.md (L663-664)
```markdown
- #3371: Activate ckb2021 in mainnet since epoch 5414 (@doitian)
    At about 2022/05/10 1:00 UTC.
```
