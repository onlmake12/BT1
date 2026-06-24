Audit Report

## Title
Sync Layer `version_check` Ignores RFC-0048 Hardfork Gate, Permanently Rejecting Valid Post-Hardfork Non-Zero-Version Headers — (`sync/src/synchronizer/headers_process.rs`)

## Summary

`HeaderAcceptor::version_check` unconditionally rejects any header whose `version` field is not `0`, with no reference to the RFC-0048 hardfork switch (`is_remove_header_version_reservation_rule_enabled`). RFC-0048 is active on mainnet (epoch ≥ 12293) and explicitly lifts the version-zero reservation. The hardfork gate function is defined via macro but confirmed by grep to have exactly one occurrence in the entire codebase — its definition site — meaning it is never invoked. Any node receiving a syntactically valid, PoW-correct block with a non-zero header version will permanently mark it `BLOCK_INVALID` in the shared block-status map and never reconsider it, causing a consensus split against nodes that correctly implement RFC-0048.

## Finding Description

`HeaderAcceptor::version_check` at `sync/src/synchronizer/headers_process.rs:286–293` enforces a hardcoded version-zero rule with no epoch or hardfork awareness:

```rust
pub fn version_check(&self, state: &mut ValidationResult) -> Result<(), ()> {
    if self.header.version() != 0 {
        state.invalid(Some(ValidationError::Version));
        Err(())
    } else {
        Ok(())
    }
}
``` [1](#0-0) 

When this returns `Err(())`, `accept()` immediately writes `BlockStatus::BLOCK_INVALID` into the shared block-status map and returns, permanently blacklisting the block hash: [2](#0-1) 

The RFC-0048 hardfork gate is defined in `util/types/src/core/hardfork/ckb2023.rs` via the `define_methods!` macro, generating `is_remove_header_version_reservation_rule_enabled`: [3](#0-2) 

It is activated on mainnet at `CKB2023_START_EPOCH` (epoch 12293): [4](#0-3) 

A codebase-wide grep for `is_remove_header_version_reservation_rule_enabled` returns exactly one match — the macro definition site — confirming the function is never called anywhere.

The consensus-layer verifiers provide no safety net. `HeaderVerifier::verify()` checks only PoW, block number, epoch continuity, and timestamp: [5](#0-4) 

`BlockVerifier::verify()` checks proposals limit, block bytes, cellbase, duplicates, and merkle root — no version field: [6](#0-5) 

The execution order in `accept()` is: `prev_block_check` → `non_contextual_check` (PoW + structural) → `version_check`. A header must pass valid PoW before reaching `version_check`, so the triggering block must be a legitimately mined block with non-zero version — exactly the class of blocks RFC-0048 permits. [7](#0-6) 

## Impact Explanation

This is a **consensus deviation** vulnerability. After RFC-0048 activation (epoch ≥ 12293, already live on mainnet), a miner is permitted by consensus rules to produce a block with a non-zero `version` field. Any CKB node running this code will permanently reject such a block at the sync layer, diverging from nodes that correctly implement RFC-0048. The affected node silently stalls on a stale fork with no operator-visible error beyond a debug log line. This maps directly to the Critical bounty impact: **"Vulnerabilities which could easily cause consensus deviation."**

## Likelihood Explanation

RFC-0048 is already active on mainnet. No current miner software produces non-zero-version headers, so the bug is latent today. The trigger conditions are:

1. A miner updates block-assembly software to set a non-zero `version` field (a natural use of the now-unreserved bits under RFC-0048).
2. Any peer relaying that block to a node running this code causes permanent `BLOCK_INVALID` marking.

The "malicious relayer" path (crafting a fake header with `version = 1`) requires a valid PoW solution because `non_contextual_check` (which includes `PowVerifier`) runs before `version_check`. This makes opportunistic DoS via crafted headers computationally expensive. The primary realistic trigger is a legitimate miner adopting non-zero versions, at which point every unpatched node splits from the canonical chain.

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

This mirrors the existing pattern used for VM-version and syscall gating in `script/src/types.rs`, where `is_vm_version_2_and_syscalls_3_enabled` is consulted before applying post-hardfork rules.

## Proof of Concept

1. Confirm RFC-0048 is active: call `get_consensus` RPC and verify `hardfork_features` contains `{"rfc":"0048","epoch_number":"0x3005"}`.
2. Mine (or obtain) a block at epoch ≥ 12293 with `header.version = 1` and a valid Eaglesong PoW solution for the current chain tip.
3. Relay the block to an unpatched CKB node via the Sync protocol `SendHeaders` P2P message.
4. Observe that `HeaderAcceptor::accept()` at `sync/src/synchronizer/headers_process.rs:346` calls `version_check`, which returns `Err(())`, causing `shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID)` at line 352.
5. Confirm the node never syncs this block even if it becomes the canonical chain tip, while a patched node or a node implementing RFC-0048 correctly accepts it — demonstrating the consensus split.

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

**File:** sync/src/synchronizer/headers_process.rs (L295-358)
```rust
    pub fn accept(&self) -> ValidationResult {
        let mut result = ValidationResult::default();
        let sync_shared = self.active_chain.sync_shared();
        let state = self.active_chain.state();
        let shared = sync_shared.shared();

        // FIXME If status == BLOCK_INVALID then return early. But which error
        // type should we return?
        let status = self.active_chain.get_block_status(&self.header.hash());
        if status.contains(BlockStatus::HEADER_VALID) {
            let header_index = sync_shared
                .get_header_index_view(
                    &self.header.hash(),
                    status.contains(BlockStatus::BLOCK_STORED),
                )
                .unwrap_or_else(|| {
                    panic!(
                        "header {}-{} with HEADER_VALID should exist",
                        self.header.number(),
                        self.header.hash()
                    )
                })
                .as_header_index();
            state
                .peers()
                .may_set_best_known_header(self.peer, header_index);
            return result;
        }

        if self.prev_block_check(&mut result).is_err() {
            debug!(
                "HeadersProcess rejected invalid-parent header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            return result;
        }

        if let Some(is_invalid) = self.non_contextual_check(&mut result).err() {
            debug!(
                "HeadersProcess rejected non-contextual header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            if is_invalid {
                shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            }
            return result;
        }

        if self.version_check(&mut result).is_err() {
            debug!(
                "HeadersProcess rejected invalid-version header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            return result;
        }

        sync_shared.insert_valid_header(self.peer, self.header);
        result
    }
```

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
