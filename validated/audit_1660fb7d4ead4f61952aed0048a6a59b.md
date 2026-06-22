### Title
`version_check()` Ignores rfc0048 Hardfork Activation, Unconditionally Rejects Non-Zero Header Versions — (`sync/src/synchronizer/headers_process.rs`)

### Summary

`HeaderAcceptor::version_check()` hardcodes `header.version() != 0` as permanently invalid. The hardfork predicate `is_remove_header_version_reservation_rule_enabled` — which is supposed to lift this restriction at epoch 9690 on testnet — is defined but **never called anywhere in the production codebase**. After rfc0048 activation, any block with a non-zero version field that passes PoW will be permanently marked `BLOCK_INVALID` and rejected by the sync layer, even though the consensus rules now permit it.

### Finding Description

`version_check()` contains no epoch or hardfork context: [1](#0-0) 

It is called unconditionally inside `accept()`, after PoW and structural checks pass: [2](#0-1) 

On failure the block hash is written to `BLOCK_INVALID` status, making the rejection permanent for that node.

Meanwhile, `is_remove_header_version_reservation_rule_enabled` is defined via the `define_methods!` macro in `CKB2023`: [3](#0-2) 

A grep across the entire repo finds this symbol **only in its definition file** — it is never consulted by `version_check()`, `HeaderVerifier`, or any other production path. `HeaderVerifier::verify()` itself performs no version check at all (only PoW, number, epoch, timestamp): [4](#0-3) 

The testnet activation epoch is hardcoded: [5](#0-4) 

### Impact Explanation

After epoch 9690 on testnet (and the corresponding epoch on mainnet), a miner that legitimately sets `header.version = 1` (as rfc0048 permits) will have every relayed block permanently blacklisted by any peer running this code. The block is inserted into `BLOCK_INVALID` status, so even a re-relay cannot recover it on that peer. This creates a consensus split: nodes that implement the full block-acceptance path correctly would accept the block, while the sync layer on all other peers would permanently reject it, causing chain fragmentation and miner reward loss.

### Likelihood Explanation

The bug is latent until a miner actually sets a non-zero version field post-activation. The hardfork infrastructure (`CKB2023_START_EPOCH = 9690`, `is_remove_header_version_reservation_rule_enabled`) is fully wired in the constant and type layers, signalling intent to use non-zero versions. The missing call site in `version_check()` is a concrete, single-function omission, not a design ambiguity.

### Recommendation

`version_check()` must be made hardfork-aware. It should accept the consensus object, derive the current epoch from the header's epoch field, and skip the version-zero assertion when `consensus.hardfork_switch.ckb2023.is_remove_header_version_reservation_rule_enabled(header.epoch().number())` returns `true`.

### Proof of Concept

1. Construct a `HeaderView` with `version = 1` and a valid epoch number `>= 9690`.
2. Solve PoW so `non_contextual_check()` passes.
3. Call `HeaderAcceptor::accept()`.
4. Observe `version_check()` returns `Err(())` and the block is marked `BLOCK_INVALID`, despite rfc0048 being active.
5. Confirm `is_remove_header_version_reservation_rule_enabled(9690)` returns `true` — proving the predicate exists but is never consulted.

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

**File:** util/constant/src/hardfork/testnet.rs (L14-14)
```rust
pub const CKB2023_START_EPOCH: u64 = 9690;
```
