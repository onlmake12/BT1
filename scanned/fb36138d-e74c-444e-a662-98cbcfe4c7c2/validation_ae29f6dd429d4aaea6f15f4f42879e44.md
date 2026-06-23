### Title
Sync Layer `version_check` Hardcodes `version != 0` as Invalid Without Fork Awareness After CKB2023 (RFC 0048) — (File: `sync/src/synchronizer/headers_process.rs`)

---

### Summary

`HeaderAcceptor::version_check` in the sync layer unconditionally rejects any block header whose version field is not zero. CKB2023 hard fork (RFC 0048, `remove_header_version_reservation_rule`) explicitly removes this reservation rule, making non-zero header versions valid after epoch 12,293 on mainnet (active since July 2025). The check is never updated to be fork-aware, so valid post-CKB2023 blocks with non-zero versions are permanently marked `BLOCK_INVALID` by the sync layer, creating a potential consensus split.

---

### Finding Description

`HeaderAcceptor::version_check` in `sync/src/synchronizer/headers_process.rs` hardcodes the requirement that `header.version() == 0`: [1](#0-0) 

This check is called unconditionally inside `accept()`, and on failure the block hash is permanently written into the shared state as `BLOCK_INVALID`: [2](#0-1) 

CKB2023 introduces RFC 0048 (`remove_header_version_reservation_rule`), which is tracked in `CKB2023` and activated at `CKB2023_START_EPOCH`: [3](#0-2) [4](#0-3) 

The mainnet CKB2023 start epoch is `12_293`, corresponding to approximately July 2025. The `HardForkConfig::complete_mainnet()` wires this into the consensus object: [5](#0-4) 

The `HeaderAcceptor` struct has full access to `active_chain`, which carries the consensus object and thus the hardfork switch. However, `version_check` never consults it — it simply hardcodes `!= 0` as invalid regardless of epoch.

Critically, the **main block verifier** (`BlockVerifier`) does **not** contain a header version check at all: [6](#0-5) 

This means the consensus layer has never enforced `version == 0` as a block validity rule. The sync layer's `version_check` was always a defense-in-depth filter. After RFC 0048 removes the reservation rule, this filter becomes incorrect: it rejects blocks that are valid by consensus, and permanently poisons their status as `BLOCK_INVALID` in the node's shared state.

---

### Impact Explanation

After CKB2023 is active (already the case on mainnet), any block header with `version != 0` that arrives via the P2P sync protocol is:

1. Rejected by `version_check` with `ValidationState::Invalid`.
2. Permanently recorded as `BlockStatus::BLOCK_INVALID` in the shared state via `shared.insert_block_status(...)`.
3. Never reconsidered, even if the block is valid by all consensus rules.

If a miner produces a block with a non-zero version field (which is now permitted by RFC 0048), nodes running this code will refuse to sync it. Nodes that have correctly implemented RFC 0048 will accept it. This creates a **consensus split**: the two sets of nodes follow different chain tips, breaking network consensus.

---

### Likelihood Explanation

CKB2023 is already active on mainnet (since approximately July 2025, epoch 12,293). The vulnerability is live. The immediate likelihood of exploitation is moderate: no miner currently produces non-zero version blocks, so there is no active split. However, any miner who begins using the version field (as RFC 0048 permits) will trigger the split. A malicious miner could deliberately do so to partition the network.

---

### Recommendation

Update `version_check` to consult the hardfork switch before enforcing the `version == 0` requirement. After RFC 0048 is active, the check should be skipped (or replaced with whatever new version rule applies). The pattern already used elsewhere in the codebase — checking `hardfork_switch.ckb2023.is_remove_header_version_reservation_rule_enabled(epoch_number)` — should be applied here. [7](#0-6) 

---

### Proof of Concept

1. CKB2023 is active on mainnet at epoch 12,293 (July 2025). RFC 0048 removes the header version reservation rule.
2. A miner constructs a valid block at any epoch ≥ 12,293 with `header.version = 1`. The block satisfies all consensus rules (PoW, epoch, merkle root, etc.). `BlockVerifier` has no version check and accepts it.
3. The block is relayed to a peer running this code via the P2P sync protocol.
4. `HeaderAcceptor::accept()` calls `version_check`, which evaluates `1 != 0 → true` and calls `state.invalid(Some(ValidationError::Version))`.
5. `shared.insert_block_status(header.hash(), BlockStatus::BLOCK_INVALID)` permanently marks the block as invalid.
6. The node never adopts this block. Nodes that correctly implement RFC 0048 do adopt it. The network splits.

### Citations

**File:** sync/src/synchronizer/headers_process.rs (L286-293)
```rust
    pub fn version_check(&self, state: &mut ValidationResult) -> Result<(), ()> {
        if self.header.version() != 0 {
            state.invalid(Some(ValidationError::Version));
            Err(())
        } else {
            Ok(())
        }
    }
```

**File:** sync/src/synchronizer/headers_process.rs (L346-354)
```rust
        if self.version_check(&mut result).is_err() {
            debug!(
                "HeadersProcess rejected invalid-version header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            return result;
        }
```

**File:** util/types/src/core/hardfork/ckb2023.rs (L66-73)
```rust
define_methods!(
    CKB2023,
    rfc_0048,
    remove_header_version_reservation_rule,
    is_remove_header_version_reservation_rule_enabled,
    disable_rfc_0048,
    "RFC PR 0048"
);
```

**File:** util/types/src/core/hardfork/ckb2023.rs (L74-81)
```rust
define_methods!(
    CKB2023,
    rfc_0049,
    vm_version_2_and_syscalls_3,
    is_vm_version_2_and_syscalls_3_enabled,
    disable_rfc_0049,
    "RFC PR 0049"
);
```

**File:** util/constant/src/hardfork/mainnet.rs (L10-14)
```rust
/// 2025-07-01 06:32:53 utc
/// |            hash                                                    |  number   |    timestamp    | epoch |
/// | 0xf959f70e487bc3073374d148ef0df713e6060542b84d89a3318bf18edbacdf94 | 15,414,776  |  1739773973982  |  11,489 (1800/1800)|
/// 1739773973982 + 804 * (4 * 60 * 60 * 1000) = 1751351573982  2024-10-25 05:43 utc
pub const CKB2023_START_EPOCH: u64 = 12_293;
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

**File:** verification/src/block_verifier.rs (L36-48)
```rust
impl<'a> Verifier for BlockVerifier<'a> {
    type Target = BlockView;

    fn verify(&self, target: &BlockView) -> Result<(), Error> {
        let max_block_proposals_limit = self.consensus.max_block_proposals_limit();
        let max_block_bytes = self.consensus.max_block_bytes();
        BlockProposalsLimitVerifier::new(max_block_proposals_limit).verify(target)?;
        BlockBytesVerifier::new(max_block_bytes).verify(target)?;
        CellbaseVerifier::new().verify(target)?;
        DuplicateVerifier::new().verify(target)?;
        MerkleRootVerifier::new().verify(target)
    }
}
```
