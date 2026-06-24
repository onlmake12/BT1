All cited code is confirmed in the repository. The claim is accurate:

- `CKB2021_START_EPOCH = 0` with `5414` commented out in mainnet.rs [1](#0-0) 
- `CKB2021_START_EPOCH = 0` with `3113` commented out in testnet.rs [2](#0-1) 
- `new_mirana()` uses the `0`-valued constant for rfc_0029/0030/0031/0036/0038 [3](#0-2) 
- `complete_mainnet()` propagates the same `0` value through `update_2021` [4](#0-3) 

---

Audit Report

## Title
Hardcoded `CKB2021_START_EPOCH = 0` Causes Consensus Divergence on Historical Block Validation - (File: `util/constant/src/hardfork/mainnet.rs`)

## Summary
`CKB2021_START_EPOCH` is set to `0` in both `util/constant/src/hardfork/mainnet.rs` and `util/constant/src/hardfork/testnet.rs`, with the historically correct values (`5414` and `3113` respectively) left only as comments. This constant is consumed by `CKB2021::new_mirana()` and `HardForkConfig::complete_mainnet/complete_testnet` to set the activation epoch for RFC 0029, 0030, 0031, 0036, and 0038. Any node syncing from genesis applies post-hardfork consensus rules to all pre-hardfork blocks, producing consensus divergence from the canonical chain.

## Finding Description
In `util/constant/src/hardfork/mainnet.rs` line 8, `CKB2021_START_EPOCH` is `0`; the correct value `5414` is commented out on line 7. The same pattern exists in `testnet.rs` (correct value `3113` commented out, `0` active). `CKB2021::new_mirana()` in `util/types/src/core/hardfork/ckb2021.rs` lines 91–96 passes this constant directly to `.rfc_0029()`, `.rfc_0030()`, `.rfc_0031()`, `.rfc_0036()`, and `.rfc_0038()`. `HardForkConfig::complete_mainnet()` in `spec/src/hardfork.rs` lines 23–27 passes `mainnet::CKB2021_START_EPOCH` to `update_2021`, which at lines 61–66 propagates it to the same five RFC fields. RFC 0028 and RFC 0032 correctly use `RFC0028_RFC0032_RFC0033_RFC0034_START_EPOCH = 5414`, confirming the split is unintentional. With `CKB2021_START_EPOCH = 0`, RFC 0030 (`check_length_in_epoch_since`) and RFC 0038 (`disallow_over_max_dep_expansion_limit`) add new rejection conditions active from genesis. Any canonical mainnet block in epochs 0–5413 whose transactions exercise these rules will be spuriously rejected by the affected node, halting IBD permanently. RFC 0031 applies new header format validation to old blocks, which can also cause spurious rejection. No existing guard compensates for the wrong epoch value.

## Impact Explanation
This directly causes consensus deviation: a node built from this repository cannot complete IBD against the canonical mainnet because it applies stricter post-hardfork validation rules to pre-hardfork historical blocks. This matches the Critical impact class: "Vulnerabilities which could easily cause consensus deviation."

## Likelihood Explanation
Every new mainnet node performing a full sync from genesis is affected. The trigger is the ordinary IBD path — no special attacker capability is required for the rejection failure mode. Any canonical mainnet peer serving historical blocks is sufficient to trigger the divergence. The acceptance failure mode (RFC 0029/0036 relaxation) additionally allows a peer operator to serve a crafted alternative chain that the affected node accepts but correctly-configured peers reject.

## Recommendation
Restore the correct activation epochs:
```rust
// util/constant/src/hardfork/mainnet.rs
pub const CKB2021_START_EPOCH: u64 = 5414;

// util/constant/src/hardfork/testnet.rs
pub const CKB2021_START_EPOCH: u64 = 3113;
```
Add a consensus regression test asserting the exact epoch number for each RFC on each named network, comparing against the historical chain-spec files used at the time of the CKB2021 upgrade.

## Proof of Concept
1. Build a CKB node from this repository configured for mainnet.
2. At startup, inspect the resolved hardfork switch: `rfc_0029`, `rfc_0030`, `rfc_0031`, `rfc_0036`, `rfc_0038` will all report activation epoch `0`.
3. Start IBD against a canonical mainnet peer. The node will reject the first historical block (epochs 0–5413) whose transactions trigger RFC 0030 (`epoch_since.index >= epoch_since.length`) or RFC 0038 (dep-expansion count above the post-hardfork limit), emitting a validation error and halting sync permanently.
4. Alternatively, write a unit test asserting `CKB2021::new_mirana().rfc_0029() == 5414`; it will fail, returning `0`.

### Citations

**File:** util/constant/src/hardfork/mainnet.rs (L7-8)
```rust
// pub const CKB2021_START_EPOCH: u64 = 5414;
pub const CKB2021_START_EPOCH: u64 = 0;
```

**File:** util/constant/src/hardfork/testnet.rs (L7-8)
```rust
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

**File:** spec/src/hardfork.rs (L23-27)
```rust
        ckb2021 = self.update_2021(
            ckb2021,
            mainnet::CKB2021_START_EPOCH,
            mainnet::RFC0028_RFC0032_RFC0033_RFC0034_START_EPOCH,
        )?;
```
