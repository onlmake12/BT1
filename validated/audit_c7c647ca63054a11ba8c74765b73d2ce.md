Audit Report

## Title
Sync Layer `version_check` Ignores RFC-0048 Hardfork Gate, Permanently Rejecting Valid Post-Hardfork Headers — (`sync/src/synchronizer/headers_process.rs`)

## Summary

`HeaderAcceptor::version_check` unconditionally rejects any header whose `version` field is not `0`, with no reference to the RFC-0048 hardfork switch (`is_remove_header_version_reservation_rule_enabled`). RFC-0048, named `remove_header_version_reservation_rule`, is active on mainnet since epoch 12,293 and is intended to lift the version-zero reservation. The generated gate function is never called anywhere in the codebase. A legitimately mined post-hardfork block carrying a non-zero header version would be permanently marked `BLOCK_INVALID` by the sync layer, causing a consensus split between nodes.

## Finding Description

The `define_methods!` macro in `util/types/src/core/hardfork/ckb2023.rs` generates `is_remove_header_version_reservation_rule_enabled(epoch_number) -> bool { epoch_number >= self.rfc_0048 }` for the `CKB2023` struct. [1](#0-0) 

This function is referenced exactly once in the entire codebase — at its macro definition site. It is never invoked.

RFC-0048 is activated on mainnet at `CKB2023_START_EPOCH = 12_293`. [2](#0-1) 

The sync layer's `HeaderAcceptor::version_check` enforces version-zero with a hardcoded comparison, consulting no hardfork state: [3](#0-2) 

When this check fails, `accept()` immediately writes `BLOCK_INVALID` into the shared block-status map — a permanent, irrecoverable state for that block hash: [4](#0-3) 

The consensus-layer verifiers contain no header version check. `HeaderVerifier::verify` checks only PoW, block number, epoch, and timestamp: [5](#0-4) 

`BlockVerifier::verify` checks proposals limit, block bytes, cellbase, duplicates, and merkle root — no version field: [6](#0-5) 

The exploit path through `accept()` is: `prev_block_check` → `non_contextual_check` (which calls `HeaderVerifier`, including PoW) → `version_check`. A header with valid PoW and `version != 0` passes the first two checks and is permanently blacklisted at the third. The consensus layer would accept the same block.

## Impact Explanation

**Critical — consensus deviation.** After RFC-0048 activation (epoch ≥ 12,293 on mainnet), a miner is permitted by consensus to produce a block with a non-zero header `version`. Any node receiving such a block via the P2P sync protocol will permanently mark it `BLOCK_INVALID` and never reconsider it, even if it becomes the canonical chain tip. Nodes that correctly implement RFC-0048 (or future clients) accept the block; all current nodes reject it. The affected node silently stalls on a stale fork with no operator-visible error beyond a debug log line.

## Likelihood Explanation

RFC-0048 is already active on mainnet. No current miner software produces non-zero version headers, so the bug is latent. Triggering it requires a miner to produce a block with `version != 0` and valid PoW — a natural action once miner software is updated to use the now-unreserved version bits. The entry path is an unprivileged P2P `SendHeaders` message; no special privilege is required. The "permanent blacklist via crafted header" sub-scenario requires valid PoW (since `non_contextual_check` runs PoW verification before `version_check` is reached), making opportunistic DoS harder but not eliminating the primary consensus-split risk from legitimately mined blocks.

## Recommendation

In `HeaderAcceptor::version_check`, consult the hardfork switch before enforcing the version-zero rule. The header's epoch number is available from `self.header.epoch().number()`, and the consensus object is reachable via `self.active_chain`:

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

This mirrors the existing pattern used for VM-version and syscall gating in `script/src/types.rs`. [3](#0-2) 

## Proof of Concept

1. Confirm RFC-0048 is active: call `get_consensus` RPC and verify `hardfork_features` contains `{"rfc":"0048","epoch_number":"0x3005"}` (epoch 12,293).
2. Construct a block header at the current chain tip with `version = 1` and a valid Eaglesong PoW solution.
3. Send a `SendHeaders` P2P message containing this header to a CKB node.
4. Observe that `HeaderAcceptor::accept()` passes `prev_block_check` and `non_contextual_check` (PoW is valid), then enters `version_check` at line 286, sets `ValidationState::Invalid`, and at line 352 writes `BlockStatus::BLOCK_INVALID` for the block hash.
5. Confirm the block hash is permanently blacklisted: re-sending the same header produces no change in node state, and the node never syncs this block even if it becomes the canonical tip on the network.

### Citations

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

**File:** util/constant/src/hardfork/mainnet.rs (L14-14)
```rust
pub const CKB2023_START_EPOCH: u64 = 12_293;
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
