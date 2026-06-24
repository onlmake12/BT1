Audit Report

## Title
Unconditional `version != 0` rejection in `HeaderAcceptor::version_check` diverges from chain verifier which performs no version check, enabling permanent consensus split — (`sync/src/synchronizer/headers_process.rs`)

## Summary
`HeaderAcceptor::version_check` unconditionally marks any block with `header.version != 0` as `BLOCK_INVALID` in the sync layer without consulting the hardfork epoch or `is_remove_header_version_reservation_rule_enabled`. The chain verification path (`HeaderVerifier` + `ContextualBlockVerifier`) performs no version check whatsoever. A block with `version=1` and valid PoW will be permanently rejected by nodes that first see it via `SendHeaders`, while nodes that receive it via the block relay path will accept it, causing an irrecoverable consensus split.

## Finding Description
In `sync/src/synchronizer/headers_process.rs`, `HeaderAcceptor::version_check` at line 286 rejects any header whose `version != 0` with no epoch guard: [1](#0-0) 

On failure, `accept()` at line 352 calls `shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID)`, permanently poisoning that block hash on the receiving node: [2](#0-1) 

`HeaderVerifier::verify` in `verification/src/header_verifier.rs` checks only PoW, parent existence, block number, epoch, and timestamp — no version field: [3](#0-2) 

`is_remove_header_version_reservation_rule_enabled` is defined via macro in `util/types/src/core/hardfork/ckb2023.rs`: [4](#0-3) 

A grep across the entire repository confirms it is referenced **only** in that definition file — it is never called in any production verifier. The chain path therefore has no version check at all, pre- or post-hardfork.

## Impact Explanation
Two nodes with identical chain state will permanently diverge if one receives a `version=1` block via `SendHeaders` and the other receives it via the block relay / chain path. The `BLOCK_INVALID` flag is permanent; the affected node will refuse the block and all descendants forever. This is a hard consensus split matching the **Critical — consensus deviation** impact class (15001–25000 points).

## Likelihood Explanation
The attacker needs to mine exactly one block with `version=1` and valid PoW. No majority hashpower is required — any miner or pool participant can produce such a block. The attacker then submits the header to a subset of peers via `SendHeaders` and the full block to others via the normal relay path. No privileged access, key material, or social engineering is required. The attack is repeatable and deterministic.

## Recommendation
`HeaderAcceptor::version_check` must be made epoch-aware. After `rfc_0048` activation it should permit `version != 0` (or be removed entirely). The fix is to consult `consensus.hardfork_switch.ckb2023.is_remove_header_version_reservation_rule_enabled(header.epoch().number())` before rejecting, mirroring the intended post-hardfork rule. Ideally, the version check should live in a single shared location called by both the sync and chain verification paths to prevent future divergence.

## Proof of Concept
1. Run two CKB nodes (A and B) on a post-epoch-12293 chain (or a devnet with `rfc_0048 = 0`).
2. Mine a block with `header.version = 1` and valid PoW.
3. Send the full block to node A via the normal `SendBlock` / compact-block relay path.
4. Send only the header to node B via `SendHeaders`.
5. Observe: node A accepts the block and advances its tip; node B marks the block hash `BLOCK_INVALID` at `sync/src/synchronizer/headers_process.rs:352` and never advances past it.
6. Assert tip disagreement and confirm node B's `BLOCK_INVALID` flag is permanent — it will refuse the block even if later sent via `SendBlock`.

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

**File:** verification/src/header_verifier.rs (L32-50)
```rust
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
