### Title
Sync Layer `version_check` Ignores RFC-0048 Hardfork Switch, Permanently Rejecting Valid Post-Hardfork Headers — (`sync/src/synchronizer/headers_process.rs`)

---

### Summary

`HeaderAcceptor::version_check` in the sync layer unconditionally rejects any header whose `version` field is not `0`, without consulting the `is_remove_header_version_reservation_rule_enabled` hardfork gate introduced by RFC-0048. RFC-0048 (`remove_header_version_reservation_rule`) is already active on mainnet and is supposed to lift the version-zero reservation. The function that implements the gate (`is_remove_header_version_reservation_rule_enabled`) is defined but is never called anywhere in the codebase. As a result, the sync layer permanently marks any post-hardfork block carrying a non-zero header version as `BLOCK_INVALID`, even though such a block is valid under the current consensus rules.

---

### Finding Description

RFC-0048 is defined in `util/types/src/core/hardfork/ckb2023.rs` and exposes `is_remove_header_version_reservation_rule_enabled(epoch)`: [1](#0-0) 

It is activated on mainnet at `CKB2023_START_EPOCH` (epoch `0x3005` = 12293, confirmed in the `get_consensus` RPC example): [2](#0-1) 

The sync layer's `HeaderAcceptor::version_check` is the **only** place in the entire codebase that enforces the header-version-must-be-zero rule. It does so with a hardcoded constant, with no reference to the hardfork switch: [3](#0-2) 

When a header fails this check, `accept()` immediately marks the block hash as `BLOCK_INVALID` in the shared block-status map and returns: [4](#0-3) 

Neither `BlockVerifier` nor `HeaderVerifier` (the consensus-layer verifiers) contain any header-version check: [5](#0-4) [6](#0-5) 

A grep for `is_remove_header_version_reservation_rule_enabled` across the entire repository returns exactly one hit — the macro definition site — confirming the function is never invoked: [1](#0-0) 

---

### Impact Explanation

After RFC-0048 activation (epoch ≥ 12293 on mainnet), a miner is permitted by consensus to produce a block whose header `version` field is non-zero. Any CKB node receiving such a block via the P2P sync protocol will:

1. Enter `HeaderAcceptor::version_check`, which unconditionally returns `Err(())` for any version ≠ 0.
2. Permanently write `BLOCK_INVALID` into the shared block-status map for that block hash.
3. Never reconsider the block, even if it is the canonical chain tip.

This creates a **consensus split**: nodes that correctly implement RFC-0048 (or future clients) accept the block; all current CKB nodes reject it via sync. The affected node is silently stuck on a stale fork with no error surfaced to the operator beyond a debug log line.

---

### Likelihood Explanation

RFC-0048 is already active on mainnet. No current miner software produces non-zero-version headers, so the bug is latent. However:

- Any miner that updates their block-assembly software to signal via the header `version` field (a natural use of the now-unreserved bits) would trigger the split.
- A malicious block relayer can craft and relay a syntactically valid header with `version = 1` to any peer, causing that peer to permanently blacklist the block hash. If the block is later mined legitimately, the peer will never sync it.

The entry path requires only an unprivileged P2P peer sending a `SendHeaders` message — no special privilege is needed.

---

### Recommendation

In `HeaderAcceptor::version_check`, consult the hardfork switch before enforcing the version-zero rule. The epoch of the header under validation is available from `self.header.epoch().number()`, and the consensus object is reachable through `self.active_chain`:

```rust
pub fn version_check(&self, state: &mut ValidationResult) -> Result<(), ()> {
    let epoch = self.header.epoch().number();
    let hardfork = self.active_chain.sync_shared()
        .shared()
        .consensus()
        .hardfork_switch();
    if !hardfork.ckb2023.is_remove_header_version_reservation_rule_enabled(epoch)
        && self.header.version() != 0
    {
        state.invalid(Some(ValidationError::Version));
        return Err(());
    }
    Ok(())
}
```

This mirrors the pattern already used for VM-version and syscall gating in `script/src/types.rs`: [7](#0-6) 

---

### Proof of Concept

1. RFC-0048 is active on mainnet (epoch ≥ 12293). Confirm with `get_consensus` RPC: `hardfork_features` shows `{"rfc":"0048","epoch_number":"0x3005"}`.
2. Construct a block header with `version = 1` and a valid PoW solution for the current tip (or relay a crafted `SendHeaders` P2P message containing such a header).
3. Send the message to a CKB node via the Sync protocol.
4. Observe that `HeaderAcceptor::version_check` fires at `sync/src/synchronizer/headers_process.rs:287`, sets `BLOCK_INVALID`, and the node never syncs the block — even though the consensus-layer `BlockVerifier` and `HeaderVerifier` contain no version check and would accept it. [3](#0-2) [1](#0-0)

### Citations

**File:** util/types/src/core/hardfork/ckb2023.rs (L40-47)
```rust
    pub fn new_mirana() -> Self {
        // Use a builder to ensure all features are set manually.
        Self::new_builder()
            .rfc_0048(hardfork::mainnet::CKB2023_START_EPOCH)
            .rfc_0049(hardfork::mainnet::CKB2023_START_EPOCH)
            .build()
            .unwrap()
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

**File:** verification/src/block_verifier.rs (L39-47)
```rust
    fn verify(&self, target: &BlockView) -> Result<(), Error> {
        let max_block_proposals_limit = self.consensus.max_block_proposals_limit();
        let max_block_bytes = self.consensus.max_block_bytes();
        BlockProposalsLimitVerifier::new(max_block_proposals_limit).verify(target)?;
        BlockBytesVerifier::new(max_block_bytes).verify(target)?;
        CellbaseVerifier::new().verify(target)?;
        DuplicateVerifier::new().verify(target)?;
        MerkleRootVerifier::new().verify(target)
    }
```

**File:** verification/src/header_verifier.rs (L30-50)
```rust
impl<'a, DL: HeaderFieldsProvider> Verifier for HeaderVerifier<'a, DL> {
    type Target = HeaderView;
    fn verify(&self, header: &Self::Target) -> Result<(), Error> {
        // POW check first
        PowVerifier::new(header, self.consensus.pow_engine().as_ref()).verify()?;
        let parent_fields = self
            .data_loader
            .get_header_fields(&header.parent_hash())
            .ok_or_else(|| UnknownParentError {
                parent_hash: header.parent_hash(),
            })?;
        NumberVerifier::new(parent_fields.number, header).verify()?;
        EpochVerifier::new(parent_fields.epoch, header).verify()?;
        TimestampVerifier::new(
            self.data_loader,
            header,
            self.consensus.median_time_block_count(),
        )
        .verify()?;
        Ok(())
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
