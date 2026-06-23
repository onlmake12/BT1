### Title
Tx-Pool Cache Invalidation Omits CKB2023 VM Version Boundary, Enabling Stale-Cache Consensus Split - (File: `util/types/src/core/hardfork/ckb2021.rs`)

---

### Summary

`CKB2021::script_result_changed_at()` tracks only the CKB2021 VM-version-1 activation epoch (`rfc_0032`) for tx-pool cache invalidation. The analogous CKB2023 VM-version-2 activation epoch (`rfc_0049`) is never registered. At the CKB2023 hardfork boundary, the tx-pool will not flush stale cached script-verification results, allowing a transaction sender to pre-submit a transaction whose cached VM-V1 result diverges from its correct VM-V2 result, causing a miner to assemble an invalid block that other nodes reject — a consensus split.

---

### Finding Description

CKB's tx-pool caches script execution results to avoid re-running scripts on every block-template assembly. When a hardfork changes which VM version executes a script, cached results from the old VM version become invalid. The mechanism to trigger a cache flush is `script_result_changed_at()`.

In `util/types/src/core/hardfork/ckb2021.rs`, this method is defined as:

```rust
pub fn script_result_changed_at(&self) -> Vec<EpochNumber> {
    let mut epochs = vec![self.rfc_0032()];
    // In future, there could be more than one epoch,
    // we should merge the same epochs and sort all epochs.
    //epochs.sort_unstable();
    //epochs.dedup();
    epochs.retain(|&x| x != 0);
    epochs
}
``` [1](#0-0) 

This only returns `rfc_0032` — the CKB2021 VM-V1 activation epoch. The `CKB2023` struct has no `script_result_changed_at()` method at all: [2](#0-1) 

And `HardForks` has no combined version of this method: [3](#0-2) 

The CKB2023 hardfork introduces `rfc_0049` (`vm_version_2_and_syscalls_3`), which changes script execution for `Type`-hash scripts and `Data2`-hash scripts from VM-V1 to VM-V2: [4](#0-3) 

The VM version selection is epoch-gated: [5](#0-4) 

On mainnet, `rfc_0049` activates at epoch `12_293`: [6](#0-5) 

Because `rfc_0049` is absent from `script_result_changed_at()`, the tx-pool will not flush its cached verification results when epoch 12,293 is crossed. The commented-out code in `script_result_changed_at()` even acknowledges this was intended to be extended:

```rust
// In future, there could be more than one epoch,
// we should merge the same epochs and sort all epochs.
//epochs.sort_unstable();
//epochs.dedup();
``` [7](#0-6) 

This is the same class of bug that was fixed for CKB2021 in CHANGELOG entry `#3090: Tx-pool refreshes caches with the incorrect VM version after hardfork`. The fix was applied only to `CKB2021`; `CKB2023` was introduced later without the same treatment.

---

### Impact Explanation

At the CKB2023 epoch boundary, the tx-pool retains cached script-verification results computed under VM-V1 rules. A miner calling `get_block_template` will receive a block template containing transactions whose cached results are stale. If any such transaction's script behaves differently under VM-V2 (e.g., a script that passes VM-V1 but fails VM-V2, or vice versa), the assembled block will be invalid under the post-fork consensus rules. Other nodes that re-verify the block from scratch using the correct VM-V2 will reject it, causing a consensus split between the miner and the rest of the network.

---

### Likelihood Explanation

The CKB2023 hardfork is scheduled for mainnet epoch 12,293. Any transaction sender who submits a transaction before the hardfork that exploits a behavioral difference between VM-V1 and VM-V2 (e.g., a script that uses new VM-V2 syscalls or ISA extensions that alter execution outcome) can trigger this. The window is the proposal-to-commit interval around the hardfork epoch. The attacker needs only to submit a valid RPC transaction — no privileged access required.

---

### Recommendation

Add `rfc_0049` to the cache-invalidation epoch list. The cleanest fix is to add a `script_result_changed_at()` method to `CKB2023` mirroring the one in `CKB2021`, and then combine both in `HardForks::script_result_changed_at()` so the tx-pool flushes at both the CKB2021 and CKB2023 VM-version boundaries.

---

### Proof of Concept

1. Before epoch 12,293 on mainnet, submit a transaction whose lock script uses `hash_type: Type` and whose script binary behaves differently under VM-V1 vs VM-V2 (e.g., uses a new syscall that returns a different value in VM-V2). The tx-pool accepts and caches the VM-V1 result.
2. At epoch 12,293, `script_result_changed_at()` returns only `[5414]` (rfc_0032), so no cache flush occurs.
3. A miner calls `get_block_template`. The tx-pool returns the transaction with its stale VM-V1 cached result.
4. The miner assembles and submits the block.
5. Other nodes verify the block from scratch using VM-V2 (correct for epoch ≥ 12,293) and reject it.
6. The miner's block is orphaned; a consensus split occurs for the duration of the miner's chain tip divergence.

### Citations

**File:** util/types/src/core/hardfork/ckb2021.rs (L118-126)
```rust
    pub fn script_result_changed_at(&self) -> Vec<EpochNumber> {
        let mut epochs = vec![self.rfc_0032()];
        // In future, there could be more than one epoch,
        // we should merge the same epochs and sort all epochs.
        //epochs.sort_unstable();
        //epochs.dedup();
        epochs.retain(|&x| x != 0);
        epochs
    }
```

**File:** util/types/src/core/hardfork/ckb2023.rs (L1-64)
```rust
use crate::core::EpochNumber;
use ckb_constant::hardfork;
use paste::paste;

/// A switch to select hard fork features base on the epoch number.
///
/// For safety, all fields are private and not allowed to update.
/// This structure can only be constructed by [`CKB2023Builder`].
///
/// [`CKB2023Builder`]:  struct.CKB2023Builder.html
#[derive(Debug, Clone)]
pub struct CKB2023 {
    rfc_0048: EpochNumber,
    rfc_0049: EpochNumber,
}

/// Builder for [`CKB2023`].
///
/// [`CKB2023`]:  struct.CKB2023.html
#[derive(Debug, Clone, Default)]
pub struct CKB2023Builder {
    rfc_0048: Option<EpochNumber>,
    rfc_0049: Option<EpochNumber>,
}

impl CKB2023 {
    /// Creates a new builder to build an instance.
    pub fn new_builder() -> CKB2023Builder {
        Default::default()
    }

    /// Creates a new builder based on the current instance.
    pub fn as_builder(&self) -> CKB2023Builder {
        Self::new_builder()
            .rfc_0048(self.rfc_0048())
            .rfc_0049(self.rfc_0049())
    }

    /// Creates a new mirana instance.
    pub fn new_mirana() -> Self {
        // Use a builder to ensure all features are set manually.
        Self::new_builder()
            .rfc_0048(hardfork::mainnet::CKB2023_START_EPOCH)
            .rfc_0049(hardfork::mainnet::CKB2023_START_EPOCH)
            .build()
            .unwrap()
    }

    /// Creates a new dev instance.
    pub fn new_dev_default() -> Self {
        // Use a builder to ensure all features are set manually.
        Self::new_builder().rfc_0048(0).rfc_0049(0).build().unwrap()
    }

    /// Creates a new instance with specified.
    pub fn new_with_specified(epoch: EpochNumber) -> Self {
        // Use a builder to ensure all features are set manually.
        Self::new_builder()
            .rfc_0048(epoch)
            .rfc_0049(epoch)
            .build()
            .unwrap()
    }
}
```

**File:** util/types/src/core/hardfork/mod.rs (L1-36)
```rust
//! Hardfork-related configuration

#[macro_use]
pub(crate) mod helper;
mod ckb2021;
mod ckb2023;

pub use ckb2021::{CKB2021, CKB2021Builder};
pub use ckb2023::{CKB2023, CKB2023Builder};

/// Hardfork-related configuration
#[derive(Debug, Clone)]
pub struct HardForks {
    /// ckb 2021 configuration
    pub ckb2021: CKB2021,
    /// ckb 2023 configuration
    pub ckb2023: CKB2023,
}

impl HardForks {
    /// construct mirana configuration
    pub fn new_mirana() -> HardForks {
        HardForks {
            ckb2021: CKB2021::new_mirana(),
            ckb2023: CKB2023::new_mirana(),
        }
    }

    /// construct dev configuration
    pub fn new_dev() -> HardForks {
        HardForks {
            ckb2021: CKB2021::new_dev_default(),
            ckb2023: CKB2023::new_dev_default(),
        }
    }
}
```

**File:** script/src/types.rs (L887-897)
```rust
    fn is_vm_version_2_and_syscalls_3_enabled(&self) -> bool {
        // If the proposal window is allowed to prejudge on the vm version,
        // it will cause proposal tx to start a new vm in the blocks before hardfork,
        // destroying the assumption that the transaction execution only uses the old vm
        // before hardfork, leading to unexpected network splits.
        let epoch_number = self.tx_env.epoch_number_without_proposal_window();
        let hardfork_switch = self.consensus.hardfork_switch();
        hardfork_switch
            .ckb2023
            .is_vm_version_2_and_syscalls_3_enabled(epoch_number)
    }
```

**File:** script/src/types.rs (L899-937)
```rust
    /// Returns the version of the machine based on the script and the consensus rules.
    pub fn select_version(&self, script: &Script) -> Result<ScriptVersion, ScriptError> {
        let is_vm_version_2_and_syscalls_3_enabled = self.is_vm_version_2_and_syscalls_3_enabled();
        let is_vm_version_1_and_syscalls_2_enabled = self.is_vm_version_1_and_syscalls_2_enabled();
        let script_hash_type = ScriptHashType::try_from(script.hash_type())
            .map_err(|err| ScriptError::InvalidScriptHashType(err.to_string()))?;
        match script_hash_type {
            ScriptHashType::Data => Ok(ScriptVersion::V0),
            ScriptHashType::Data1 => {
                if is_vm_version_1_and_syscalls_2_enabled {
                    Ok(ScriptVersion::V1)
                } else {
                    Err(ScriptError::InvalidVmVersion(1))
                }
            }
            ScriptHashType::Data2 => {
                if is_vm_version_2_and_syscalls_3_enabled {
                    Ok(ScriptVersion::V2)
                } else {
                    Err(ScriptError::InvalidVmVersion(2))
                }
            }
            ScriptHashType::Type => {
                if is_vm_version_2_and_syscalls_3_enabled {
                    Ok(ScriptVersion::V2)
                } else if is_vm_version_1_and_syscalls_2_enabled {
                    Ok(ScriptVersion::V1)
                } else {
                    Ok(ScriptVersion::V0)
                }
            }
            hash_type => {
                return Err(ScriptError::InvalidScriptHashType(format!(
                    "The ScriptHashType/{:?} has not been activated, and is not permitted for use.",
                    hash_type
                )));
            }
        }
    }
```

**File:** util/constant/src/hardfork/mainnet.rs (L1-14)
```rust
/// The Chain Specification name.
pub const CHAIN_SPEC_NAME: &str = "ckb";

/// hardcode rfc0028/rfc0032/rfc0033/rfc0034 epoch
pub const RFC0028_RFC0032_RFC0033_RFC0034_START_EPOCH: u64 = 5414;
/// First epoch number for CKB v2021, at about 2022/05/10 1:00 UTC
// pub const CKB2021_START_EPOCH: u64 = 5414;
pub const CKB2021_START_EPOCH: u64 = 0;

/// 2025-07-01 06:32:53 utc
/// |            hash                                                    |  number   |    timestamp    | epoch |
/// | 0xf959f70e487bc3073374d148ef0df713e6060542b84d89a3318bf18edbacdf94 | 15,414,776  |  1739773973982  |  11,489 (1800/1800)|
/// 1739773973982 + 804 * (4 * 60 * 60 * 1000) = 1751351573982  2024-10-25 05:43 utc
pub const CKB2023_START_EPOCH: u64 = 12_293;
```
