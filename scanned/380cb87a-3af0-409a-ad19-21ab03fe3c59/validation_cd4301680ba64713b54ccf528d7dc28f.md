Audit Report

## Title
`CKB2021_START_EPOCH` Set to `0` Instead of Historical Activation Epoch Causes Consensus Divergence — (File: `util/constant/src/hardfork/mainnet.rs`, `util/constant/src/hardfork/testnet.rs`)

## Summary

`CKB2021_START_EPOCH` is set to `0` in both `util/constant/src/hardfork/mainnet.rs` and `util/constant/src/hardfork/testnet.rs`, while the historically correct values (`5414` for mainnet, `3113` for testnet) are commented out. This causes RFC 0030 and RFC 0038 stricter validation rules to be applied from genesis, causing any node built from this code to reject historical blocks that were valid under pre-2021 consensus rules, producing a hard consensus split against the real mainnet and testnet chains.

## Finding Description

**Root cause — wrong constant values:** [1](#0-0) [2](#0-1) 

Both files have the correct historical values commented out and replaced with `0`.

**Code path 1 — `CKB2021::new_mirana()`** passes `CKB2021_START_EPOCH` (= `0`) as the activation epoch for RFC 0029, 0030, 0031, 0036, and 0038: [3](#0-2) 

**Code path 2 — `HardForkConfig::complete_mainnet()`** also passes `mainnet::CKB2021_START_EPOCH` (= `0`) to `update_2021`, which sets the same RFC fields: [4](#0-3) 

**Propagation — `ConsensusBuilder::new()`** sets `hardfork_switch: HardForks::new_mirana()` as the default, meaning every mainnet consensus instance inherits the wrong activation epoch: [5](#0-4) 

**CHANGELOG confirms the correct activation epochs:** [6](#0-5) 

The two RFCs with **stricter** rules are the critical ones:
- **RFC 0030**: Rejects transactions where `since` epoch `index >= length`. Historical mainnet blocks in epochs 0–5413 were produced under pre-2021 rules that allowed such values. With `CKB2021_START_EPOCH = 0`, this check is applied from genesis, causing rejection of previously valid historical transactions.
- **RFC 0038**: Enforces a dep expansion limit. Historical transactions exceeding this limit (valid pre-2021) are rejected when this rule is applied from genesis.

There are no runtime guards, assertions, or tests that cross-check `CKB2021_START_EPOCH` against the known historical activation epoch.

## Impact Explanation

This is a **Critical** vulnerability matching the allowed impact: *"Vulnerabilities which could easily cause consensus deviation."* A node built from this codebase applies stricter validation rules (RFC 0030, RFC 0038) to all historical blocks from genesis. Any historical mainnet block in epochs 0–5413 containing a transaction that was valid under pre-2021 rules but invalid under RFC 0030 or RFC 0038 will be rejected. The node cannot sync the canonical mainnet chain from genesis, producing a hard consensus split. The node is unable to participate correctly in mainnet consensus.

## Likelihood Explanation

Any operator who builds and runs this node against mainnet or testnet will encounter the divergence during initial chain sync, as soon as a historical block exercises the affected validation paths. No special attacker capability is required — the bug is triggered automatically by syncing the canonical chain. The root cause is a production constant file with the correct value commented out and replaced with `0`, with no runtime guard to catch the mismatch.

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
2. Start syncing from genesis. The node initializes `HardForks::new_mirana()` with `rfc_0030 = 0` and `rfc_0038 = 0`.
3. When the node processes any historical mainnet block in epochs 0–5413 containing a transaction with a `since` epoch field where `index >= length` (valid pre-2021, invalid under RFC 0030), the node rejects the block.
4. The node diverges from the canonical mainnet chain and cannot complete sync.

To confirm without running a full node: write a unit test that constructs `CKB2021::new_mirana()` and asserts `rfc_0030() == 5414` — the test will fail, returning `0` instead.

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

**File:** spec/src/hardfork.rs (L21-33)
```rust
    pub fn complete_mainnet(&self) -> Result<HardForks, String> {
        let mut ckb2021 = CKB2021::new_builder();
        ckb2021 = self.update_2021(
            ckb2021,
            mainnet::CKB2021_START_EPOCH,
            mainnet::RFC0028_RFC0032_RFC0033_RFC0034_START_EPOCH,
        )?;

        Ok(HardForks {
            ckb2021: ckb2021.build()?,
            ckb2023: CKB2023::new_mirana().as_builder().build()?,
        })
    }
```

**File:** spec/src/consensus.rs (L298-298)
```rust
                hardfork_switch: HardForks::new_mirana(),
```

**File:** CHANGELOG.md (L663-664)
```markdown
- #3371: Activate ckb2021 in mainnet since epoch 5414 (@doitian)
    At about 2022/05/10 1:00 UTC.
```
