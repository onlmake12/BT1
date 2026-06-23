### Title
Incorrect Hardcoded `CKB2021_START_EPOCH = 0` Constant Causes Consensus Divergence on Historical Block Validation - (File: `util/constant/src/hardfork/mainnet.rs`)

---

### Summary

The constant `CKB2021_START_EPOCH` in both `util/constant/src/hardfork/mainnet.rs` and `util/constant/src/hardfork/testnet.rs` is set to `0` instead of the historically correct activation epoch (`5414` for mainnet, `3113` for testnet). This constant is consumed by `CKB2021::new_mirana()` and `HardForkConfig::complete_mainnet()` to set the activation epoch for five CKB2021 hardfork rules: RFC 0029, RFC 0030, RFC 0031, RFC 0036, and RFC 0038. With the constant at `0`, these rules are treated as active from genesis, causing any node syncing from genesis to apply post-hardfork consensus rules to pre-hardfork historical blocks, producing a different view of chain validity than the actual mainnet.

---

### Finding Description

In `util/constant/src/hardfork/mainnet.rs`, the commented-out line explicitly records the intended value:

```rust
// pub const CKB2021_START_EPOCH: u64 = 5414;
pub const CKB2021_START_EPOCH: u64 = 0;
``` [1](#0-0) 

The same pattern appears in testnet:

```rust
// pub const CKB2021_START_EPOCH: u64 = 3113;
pub const CKB2021_START_EPOCH: u64 = 0;
``` [2](#0-1) 

`CKB2021_START_EPOCH` is consumed in `CKB2021::new_mirana()` to configure five RFC activation epochs:

```rust
.rfc_0029(hardfork::mainnet::CKB2021_START_EPOCH)   // should be 5414, is 0
.rfc_0030(hardfork::mainnet::CKB2021_START_EPOCH)   // should be 5414, is 0
.rfc_0031(hardfork::mainnet::CKB2021_START_EPOCH)   // should be 5414, is 0
.rfc_0036(hardfork::mainnet::CKB2021_START_EPOCH)   // should be 5414, is 0
.rfc_0038(hardfork::mainnet::CKB2021_START_EPOCH)   // should be 5414, is 0
``` [3](#0-2) 

Note that RFC 0028 and RFC 0032 correctly use `RFC0028_RFC0032_RFC0033_RFC0034_START_EPOCH = 5414`, confirming the split is unintentional for the remaining five RFCs. [4](#0-3) 

The same incorrect constant flows through `HardForkConfig::complete_mainnet()`:

```rust
ckb2021 = self.update_2021(
    ckb2021,
    mainnet::CKB2021_START_EPOCH,                        // 0 — wrong
    mainnet::RFC0028_RFC0032_RFC0033_RFC0034_START_EPOCH, // 5414 — correct
)?;
``` [5](#0-4) 

`update_2021` then propagates the `0` value to `rfc_0029`, `rfc_0030`, `rfc_0031`, `rfc_0036`, and `rfc_0038`: [6](#0-5) 

The five affected rules and their consensus impact when incorrectly active from epoch 0:

| RFC | Rule | Effect of premature activation |
|-----|------|-------------------------------|
| 0029 | Allow multiple cell-dep matches when non-ambiguous | Accepts historical txs that old nodes rejected |
| 0030 | Enforce `index < length` in epoch-since field | Rejects historical txs that old nodes accepted |
| 0031 | Reuse `uncles_hash` as `extra_hash` | Applies new header format validation to old blocks |
| 0036 | Remove header-deps immature rule | Accepts historical txs that old nodes rejected |
| 0038 | Disallow over-max dep-expansion | Rejects historical txs that old nodes accepted |

---

### Impact Explanation

A node syncing from genesis (IBD) applies the wrong consensus rules to every block in epochs 0–5413 (mainnet). Two concrete failure modes exist:

1. **Spurious rejection of valid canonical blocks**: RFC 0030 and RFC 0038 add new rejection conditions. Any historical block containing a transaction with `epoch_since.index >= epoch_since.length` (RFC 0030) or exceeding the dep-expansion limit (RFC 0038) would be rejected by the affected node, even though the original mainnet accepted it. The node cannot complete IBD and is permanently partitioned from the canonical chain.

2. **Spurious acceptance of invalid historical blocks**: RFC 0029 and RFC 0036 relax rejection conditions. A crafted fork chain containing pre-epoch-5414 blocks with non-ambiguous multiple cell-dep matches or header-deps that violate the old immature rule would be accepted by the affected node but rejected by correctly-configured peers, enabling a chain-split attack.

Both outcomes constitute consensus divergence reachable without any privileged access.

---

### Likelihood Explanation

Every new mainnet node that performs a full sync from genesis is affected. The trigger is the ordinary IBD sync path: an unprivileged sync peer simply serves canonical chain blocks. No special transaction crafting is required for the rejection case (failure mode 1); the node will diverge as soon as it encounters the first historical block whose transactions exercise the affected rules. The acceptance case (failure mode 2) requires a peer to serve a crafted alternative chain, which is within reach of any peer operator.

---

### Recommendation

Restore the correct activation epoch for both networks:

```rust
// util/constant/src/hardfork/mainnet.rs
- pub const CKB2021_START_EPOCH: u64 = 0;
+ pub const CKB2021_START_EPOCH: u64 = 5414;

// util/constant/src/hardfork/testnet.rs
- pub const CKB2021_START_EPOCH: u64 = 0;
+ pub const CKB2021_START_EPOCH: u64 = 3113;
```

Verify that `CKB2021::new_mirana()` and `HardForkConfig::complete_mainnet/complete_testnet` produce the same epoch assignments as the historical chain-spec files used at the time of the CKB2021 upgrade, and add a consensus regression test that asserts the exact epoch numbers for each RFC on each named network.

---

### Proof of Concept

1. Build a CKB node from this repository and configure it for mainnet.
2. Inspect the resolved hardfork switch at startup — `rfc_0029`, `rfc_0030`, `rfc_0031`, `rfc_0036`, `rfc_0038` will all report activation epoch `0`.
3. Start IBD against a canonical mainnet peer. Locate any mainnet block in epochs 0–5413 whose cellbase or transactions exercise RFC 0030 (epoch-since with `index >= length`) or RFC 0038 (dep-expansion count above the post-hardfork limit). The node will emit a validation error and refuse to attach the block, halting sync permanently.
4. Alternatively, construct a synthetic chain where a pre-epoch-5414 block contains a transaction with non-ambiguous duplicate cell-dep entries (valid under RFC 0029 but invalid under the original rules). Serve this chain to the affected node; it will accept the block while a correctly-configured peer rejects it, demonstrating the chain-split surface.

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

**File:** util/types/src/core/hardfork/ckb2021.rs (L90-96)
```rust
            .rfc_0028(hardfork::mainnet::RFC0028_RFC0032_RFC0033_RFC0034_START_EPOCH)
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

**File:** spec/src/hardfork.rs (L59-66)
```rust
        let builder = builder
            .rfc_0028(rfc_0028_0032_0033_0034_start)
            .rfc_0029(ckb2021)
            .rfc_0030(ckb2021)
            .rfc_0031(ckb2021)
            .rfc_0032(rfc_0028_0032_0033_0034_start)
            .rfc_0036(ckb2021)
            .rfc_0038(ckb2021);
```
