Audit Report

## Title
`version_check()` Unconditionally Rejects Non-Zero Header Versions Without Consulting rfc0048 Hardfork Switch — (`sync/src/synchronizer/headers_process.rs`)

## Summary
`HeaderAcceptor::version_check()` hardcodes rejection of any header with `version != 0` and never consults the `is_remove_header_version_reservation_rule_enabled` hardfork predicate. That predicate is defined in `util/types/src/core/hardfork/ckb2023.rs` but confirmed by exhaustive grep to appear in exactly one file — its definition — and is never invoked anywhere in the production codebase. After testnet epoch 9690 (2024-10-25, already past), any block with `header.version = 1` permitted by rfc0048 will be permanently marked `BLOCK_INVALID` on every peer running this code, causing an irrecoverable consensus split.

## Finding Description
`version_check()` at [1](#0-0)  contains a bare `header.version() != 0` predicate with no epoch context, no consensus object, and no hardfork switch consultation. It is called unconditionally inside `accept()` at [2](#0-1)  after PoW and structural checks pass. On failure, `shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID)` is written at line 352, making the rejection permanent and unrecoverable for that peer without a restart and re-sync.

The hardfork predicate `is_remove_header_version_reservation_rule_enabled` is generated via `define_methods!` at [3](#0-2)  and appears in exactly one file — its definition. It is never consulted by `version_check()`, `HeaderVerifier::verify()`, or any other production path.

`HeaderVerifier::verify()` at [4](#0-3)  performs only PoW, number, epoch, and timestamp checks — no version check — so there is no alternative path that would catch or permit a non-zero version.

By contrast, the analogous rfc0049 predicate `is_vm_version_2_and_syscalls_3_enabled` is actively used in `script/src/types.rs` (5 call sites), confirming the intended pattern: hardfork predicates are supposed to gate behavior, but the rfc0048 call site was never added.

The testnet activation epoch is confirmed at [5](#0-4)  as epoch 9690 (2024-10-25), which is already in the past.

Exploit path:
1. Testnet epoch 9690 has activated.
2. A miner constructs a block with `header.version = 1` (permitted by rfc0048 post-activation) and solves PoW so `non_contextual_check()` passes.
3. `version_check()` returns `Err(())` unconditionally.
4. `BlockStatus::BLOCK_INVALID` is permanently written for the block hash.
5. Nodes correctly implementing rfc0048 accept the block; nodes running this code permanently reject it — consensus split.

## Impact Explanation
**Critical — consensus deviation.** Nodes running this code will permanently blacklist any post-rfc0048 block with a non-zero version field, while correctly-implemented nodes accept it. The `BLOCK_INVALID` status is irrecoverable on the affected peer without a restart and re-sync. A single miner exercising the rfc0048-permitted behavior fragments the network into two incompatible views of the canonical chain.

## Likelihood Explanation
The hardfork activation epoch (9690) has already passed on testnet. Any miner or block producer that sets `header.version = 1` — a behavior explicitly enabled by rfc0048 — will trigger this bug on every peer running this code. No special attacker capability is required beyond normal mining. The bug is a concrete missing call site, not a design ambiguity, and is deterministically reproducible.

## Recommendation
`version_check()` must receive the consensus object and the header's epoch number, then skip the version-zero assertion when the hardfork is active. `HeaderAcceptor` already holds a `HeaderVerifier` that carries a `&Consensus` reference (confirmed at [6](#0-5) ), so threading the consensus object through requires minimal refactoring:

```rust
pub fn version_check(&self, state: &mut ValidationResult, consensus: &Consensus) -> Result<(), ()> {
    let epoch_number = self.header.epoch().number();
    if self.header.version() != 0
        && !consensus
            .hardfork_switch
            .ckb2023
            .is_remove_header_version_reservation_rule_enabled(epoch_number)
    {
        state.invalid(Some(ValidationError::Version));
        return Err(());
    }
    Ok(())
}
```

## Proof of Concept
1. Construct a `HeaderView` with `version = 1` and epoch number `>= 9690` (testnet activation).
2. Solve PoW so `PowVerifier` passes inside `non_contextual_check()`.
3. Call `HeaderAcceptor::accept()`.
4. Observe `version_check()` returns `Err(())` and `BlockStatus::BLOCK_INVALID` is inserted for the block hash at [7](#0-6) .
5. Separately call `consensus.hardfork_switch.ckb2023.is_remove_header_version_reservation_rule_enabled(9690)` and observe it returns `true` — proving the predicate exists and is active but is never consulted by `version_check()`.
6. Repeat with a patched node to confirm the block is accepted, demonstrating the split.

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

**File:** verification/src/header_verifier.rs (L15-18)
```rust
pub struct HeaderVerifier<'a, DL> {
    data_loader: &'a DL,
    consensus: &'a Consensus,
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

**File:** util/constant/src/hardfork/testnet.rs (L10-14)
```rust
/// 2024-10-25 05:43 utc
/// |            hash                                                    |  number   |    timestamp    | epoch |
/// | 0xa229cd72240f6ef238681e21d1e6884b825afce07e2394308411facfb3cd64c2 | 14,691,304  |  1727243008225  |  9510 (1800/1800)|
/// 1727243008225 + 180 * (4 * 60 * 60 * 1000) = 1729835008225  2024-10-25 05:43 utc
pub const CKB2023_START_EPOCH: u64 = 9690;
```
