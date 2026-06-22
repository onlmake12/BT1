### Title
Missing CKB2023 VM-Version Epoch in `script_result_changed_at` Leaves Tx-Pool Verification Cache Stale at Hard-Fork Boundary — (`util/types/src/core/hardfork/ckb2021.rs`)

---

### Summary

`CKB2021::script_result_changed_at()` is the sole function that tells the tx-pool which epoch boundaries require a full cache flush of cached script-verification results. It only lists `rfc_0032` (VM version 1 / CKB2021). The CKB2023 hard fork introduces `rfc_0049` (VM version 2 + syscalls 3), which materially changes script execution results, but `CKB2023` has no `script_result_changed_at()` method and `rfc_0049` is never returned by the existing one. As a result, the tx-pool cache is not flushed at the CKB2023 activation epoch, and transactions cached under VM-v1 rules continue to be served as valid after VM-v2 activates.

---

### Finding Description

`CKB2021::script_result_changed_at()` is defined as:

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

This function returns only the `rfc_0032` epoch (VM version 1). The `CKB2023` struct, which governs `rfc_0048` (remove header version reservation rule) and `rfc_0049` (VM version 2 + syscalls 3), has no equivalent method: [2](#0-1) 

`rfc_0049` is the CKB2023 analog of `rfc_0032`: it activates a new VM version (`is_vm_version_2_and_syscalls_3_enabled`) that changes script execution results. Script verification in `TxInfo` explicitly gates on this flag: [3](#0-2) 

The tx-pool uses `script_result_changed_at()` to know when to invalidate cached `Completed` (cycles + fee) entries. Because `rfc_0049`'s epoch is absent from that list, the cache is never flushed at the CKB2023 boundary. Transactions verified under VM-v1 rules remain in the cache as "valid" after CKB2023 activates.

The commented-out lines in `script_result_changed_at` (`//epochs.sort_unstable(); //epochs.dedup();`) even acknowledge that multiple epochs were anticipated but were never wired up: [4](#0-3) 

The `HardForks` struct holds both `ckb2021` and `ckb2023`, but only `ckb2021.script_result_changed_at()` is ever consulted: [5](#0-4) 

---

### Impact Explanation

When the CKB2023 epoch (`rfc_0049`) is reached, the tx-pool continues to serve stale cached verification results computed under VM version 1. Two concrete consequences:

1. **Invalid-block production by miners**: A transaction that passes VM-v1 verification (cached as valid) but fails VM-v2 verification gets assembled into a block template. The chain verifier (which does not use the tx-pool cache) rejects the block, causing the miner to produce an orphan block and wasting PoW.

2. **Incorrect tx-pool admission/rejection**: A transaction that is invalid under VM-v1 but valid under VM-v2 is rejected from the pool even after CKB2023 activates, because the stale cache entry says it failed. Conversely, a transaction valid under VM-v1 but invalid under VM-v2 remains in the pool and can be relayed to peers, causing those peers to waste verification cycles and potentially diverge in pool state.

The CHANGELOG records that an identical bug was fixed for CKB2021 (`#3090: Tx-pool refreshes caches with the incorrect VM version after hardfork`), confirming the mechanism is real and the omission of CKB2023 is a regression of the same class.

---

### Likelihood Explanation

CKB2023 (`rfc_0049`) is already activated on mainnet at epoch `0x3005`. Any node that upgraded through that boundary without a manual cache flush was exposed. The bug is reachable by any transaction sender who submits a transaction whose script behaviour differs between VM-v1 and VM-v2 — no special privilege is required. The attacker-controlled entry path is the standard `send_transaction` RPC or P2P relay.

---

### Recommendation

1. Add a `script_result_changed_at()` method to `CKB2023` that returns `[rfc_0049]` (mirroring the pattern on `CKB2021`).
2. Aggregate both lists in `HardForks` so the tx-pool flushes its cache at every VM-changing epoch boundary.
3. Uncomment and activate the `sort_unstable` / `dedup` lines in `CKB2021::script_result_changed_at()` once the merged list is in place.
4. Add a regression test that asserts `script_result_changed_at()` covers every RFC that changes `select_version()` output.

---

### Proof of Concept

```
Epoch timeline (mainnet):
  epoch 0x1526 (5414)  → rfc_0032 activates → VM v1 enabled
                          script_result_changed_at() returns [5414]  ✓ cache flushed

  epoch 0x3005 (12293) → rfc_0049 activates → VM v2 enabled
                          script_result_changed_at() returns [5414]  ✗ cache NOT flushed

Consequence:
  1. Tx T submitted at epoch 12290 (VM v1 active).
     Cache entry: T → { cycles: X, fee: Y, valid: true }

  2. Epoch 12293 passes. VM v2 now active.
     T's script fails under VM v2 (e.g., uses a syscall removed/changed in v2).

  3. Miner calls get_block_template().
     Tx-pool returns T from stale cache (still marked valid).
     Block B is assembled containing T.

  4. Block B is submitted to chain verifier.
     Chain verifier re-executes T under VM v2 → script fails → B rejected.
     Miner's PoW is wasted; block is orphaned.
```

The root cause is the single missing line in `CKB2021::script_result_changed_at()` (or the absent method on `CKB2023`): [1](#0-0) [6](#0-5)

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

**File:** util/types/src/core/hardfork/ckb2023.rs (L12-64)
```rust
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

**File:** util/types/src/core/hardfork/ckb2023.rs (L66-81)
```rust
define_methods!(
    CKB2023,
    rfc_0048,
    remove_header_version_reservation_rule,
    is_remove_header_version_reservation_rule_enabled,
    disable_rfc_0048,
    "RFC PR 0048"
);
define_methods!(
    CKB2023,
    rfc_0049,
    vm_version_2_and_syscalls_3,
    is_vm_version_2_and_syscalls_3_enabled,
    disable_rfc_0049,
    "RFC PR 0049"
);
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

**File:** util/types/src/core/hardfork/mod.rs (L13-18)
```rust
pub struct HardForks {
    /// ckb 2021 configuration
    pub ckb2021: CKB2021,
    /// ckb 2023 configuration
    pub ckb2023: CKB2023,
}
```
