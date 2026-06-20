### Title
Incorrect `CKB2021_START_EPOCH` Constant Causes Wrong Hardfork Activation Epoch on Mainnet/Testnet — (`util/constant/src/hardfork/mainnet.rs`)

---

### Summary

The constant `CKB2021_START_EPOCH` in both the mainnet and testnet hardfork constant files is set to `0` instead of the historically correct activation epoch (`5414` for mainnet, `3113` for testnet). The correct values are explicitly commented out directly above the incorrect definitions. This causes multiple CKB2021 consensus rule changes (RFC-0029, RFC-0030, RFC-0031, RFC-0036, RFC-0038) to be treated as active from genesis (epoch 0) rather than their true activation epochs. Any node built from this code will apply post-hardfork validation rules to all historical blocks, creating a divergent view of chain validity from the rest of the network.

---

### Finding Description

In `util/constant/src/hardfork/mainnet.rs`:

```rust
/// First epoch number for CKB v2021, at about 2022/05/10 1:00 UTC
// pub const CKB2021_START_EPOCH: u64 = 5414;   ← correct value, commented out
pub const CKB2021_START_EPOCH: u64 = 0;          ← incorrect value in use
``` [1](#0-0) 

The same pattern exists for testnet:

```rust
/// First epoch number for CKB v2021, at about 2021/10/24 3:15 UTC.
// pub const CKB2021_START_EPOCH: u64 = 3113;   ← correct value, commented out
pub const CKB2021_START_EPOCH: u64 = 0;          ← incorrect value in use
``` [2](#0-1) 

This constant is consumed in two production code paths:

**1. `CKB2021::new_mirana()`** — the canonical mainnet hardfork switch constructor — uses `CKB2021_START_EPOCH` to set the activation epoch for RFC-0029, RFC-0030, RFC-0031, RFC-0036, and RFC-0038:

```rust
pub fn new_mirana() -> Self {
    Self::new_builder()
        .rfc_0028(hardfork::mainnet::RFC0028_RFC0032_RFC0033_RFC0034_START_EPOCH) // 5414
        .rfc_0029(hardfork::mainnet::CKB2021_START_EPOCH)  // 0 ← wrong
        .rfc_0030(hardfork::mainnet::CKB2021_START_EPOCH)  // 0 ← wrong
        .rfc_0031(hardfork::mainnet::CKB2021_START_EPOCH)  // 0 ← wrong
        .rfc_0032(hardfork::mainnet::RFC0028_RFC0032_RFC0033_RFC0034_START_EPOCH) // 5414
        .rfc_0036(hardfork::mainnet::CKB2021_START_EPOCH)  // 0 ← wrong
        .rfc_0038(hardfork::mainnet::CKB2021_START_EPOCH)  // 0 ← wrong
        ...
}
``` [3](#0-2) 

**2. `HardForkConfig::complete_mainnet()`** — called during `build_consensus()` for mainnet chain spec — passes `mainnet::CKB2021_START_EPOCH` (= 0) as the activation epoch for the same set of RFCs:

```rust
pub fn complete_mainnet(&self) -> Result<HardForks, String> {
    let mut ckb2021 = CKB2021::new_builder();
    ckb2021 = self.update_2021(
        ckb2021,
        mainnet::CKB2021_START_EPOCH,                          // 0 ← wrong
        mainnet::RFC0028_RFC0032_RFC0033_RFC0034_START_EPOCH,  // 5414
    )?;
    ...
}
``` [4](#0-3) 

`build_consensus()` in `spec/src/lib.rs` calls `build_hardfork_switch()` which invokes `complete_mainnet()` for the mainnet chain spec, making this a live production code path. [5](#0-4) 

The affected RFCs and their rule changes:

| RFC | Rule Change | Direction |
|-----|-------------|-----------|
| RFC-0029 | Allow multiple cell dep matches on identical data | Relaxing |
| RFC-0030 | Enforce `index < length` in epoch-based `since` fields | **Tightening** |
| RFC-0031 | Reuse `uncles_hash` as `extra_hash` in block header | Structural |
| RFC-0036 | Remove header deps immature rule | Relaxing |
| RFC-0038 | Disallow over max dep expansion limit | **Tightening** |

---

### Impact Explanation

A node built from this code applies RFC-0030 and RFC-0038 validation rules to every block from epoch 0. RFC-0030 rejects transactions whose epoch-based `since` field has `index >= length`; RFC-0038 rejects transactions that exceed the dep expansion limit. Both constraints did not exist before epoch 5414 on mainnet. Any historical mainnet block (epochs 0–5413) containing a transaction that violates either rule would be rejected by this node, causing it to diverge from the canonical mainnet chain. The node would be unable to complete IBD (Initial Block Download) and would be permanently forked from the network.

Additionally, RFC-0029 and RFC-0036 relax rules from epoch 0. A node accepting these relaxed rules for historical blocks would accept transactions that the rest of the network considers invalid for those epochs, creating a second class of consensus divergence.

---

### Likelihood Explanation

The incorrect constant is in the production mainnet and testnet hardfork configuration files, not in tests or dev configs. The correct values are literally commented out one line above the wrong values, confirming this is an unintentional regression. Any operator who builds and runs a mainnet node from this codebase will be affected. The impact materializes automatically during chain sync without any special attacker action — the node simply cannot agree with the rest of the network on the validity of historical blocks.

---

### Recommendation

Restore the correct activation epoch constants:

```rust
// util/constant/src/hardfork/mainnet.rs
pub const CKB2021_START_EPOCH: u64 = 5414;

// util/constant/src/hardfork/testnet.rs
pub const CKB2021_START_EPOCH: u64 = 3113;
```

Add a compile-time or startup assertion that `CKB2021_START_EPOCH` is non-zero for mainnet/testnet configurations, to prevent future regressions of this class.

---

### Proof of Concept

1. Build a CKB node from this repository and configure it for mainnet (`id = "ckb"`).
2. Start IBD from genesis.
3. The node calls `build_consensus()` → `build_hardfork_switch()` → `complete_mainnet()`, which sets RFC-0030 and RFC-0038 active at epoch 0.
4. During sync, the node validates historical blocks (epochs 0–5413) using RFC-0030's `index < length` check and RFC-0038's dep expansion limit.
5. Any historical transaction violating these rules causes the node to reject the block and halt sync, permanently diverging from the canonical mainnet chain.
6. Compare with a correctly configured node (epoch 5414): it accepts the same historical blocks without issue, confirming the divergence is caused solely by the incorrect constant.

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

**File:** spec/src/lib.rs (L560-601)
```rust
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
