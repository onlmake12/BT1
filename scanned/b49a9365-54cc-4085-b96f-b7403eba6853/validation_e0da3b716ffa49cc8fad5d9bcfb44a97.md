Audit Report

## Title
`version_check()` Unconditionally Rejects Non-Zero Header Versions Without Consulting rfc0048 Hardfork Switch — (`sync/src/synchronizer/headers_process.rs`)

## Summary
`HeaderAcceptor::version_check()` hardcodes rejection of any header with `version != 0` and has no awareness of the rfc0048 hardfork switch (`is_remove_header_version_reservation_rule_enabled`). The hardfork predicate is defined in `util/types/src/core/hardfork/ckb2023.rs` but confirmed by exhaustive grep to appear **only in its definition file** — it is never called anywhere in the production codebase. After epoch 9690 on testnet (a timestamp already in the past per the constant file comments), any miner legitimately setting `header.version = 1` per rfc0048 will have every relayed block permanently marked `BLOCK_INVALID`, causing an irrecoverable consensus split on affected peers.

## Finding Description
`version_check()` at `sync/src/synchronizer/headers_process.rs:286-293` contains a bare `header.version() != 0` predicate with no epoch context, no consensus object, and no hardfork switch consultation:

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

It is called unconditionally inside `accept()` at L346-354, after PoW and structural checks pass. On failure, `shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID)` is written, making the rejection permanent and unrecoverable for that peer.

The hardfork predicate `is_remove_header_version_reservation_rule_enabled` is generated via `define_methods!` in `util/types/src/core/hardfork/ckb2023.rs:66-73`. A grep across the entire repository finds this symbol in **exactly one file** — its definition. It is never consulted by `version_check()`, `HeaderVerifier::verify()`, `BlockVerifier::verify()`, or any other production path.

`HeaderVerifier::verify()` (`verification/src/header_verifier.rs:32-50`) performs only PoW, number, epoch, and timestamp checks — no version check — so there is no alternative path that would catch or permit a non-zero version. `BlockVerifier::verify()` similarly performs no version check.

By contrast, the analogous rfc0049 predicate `is_vm_version_2_and_syscalls_3_enabled` is actively used in `script/src/types.rs`, confirming the intended pattern: hardfork predicates are supposed to gate behavior, but the rfc0048 call site was simply never added.

The exploit path is:
1. Testnet epoch 9690 has activated (timestamp 2024-10-25 per `util/constant/src/hardfork/testnet.rs`).
2. A miner constructs a block with `header.version = 1` (permitted by rfc0048 post-activation).
3. The miner solves PoW; `non_contextual_check()` passes.
4. `version_check()` returns `Err(())` unconditionally.
5. The block hash is written to `BLOCK_INVALID`; the rejection is permanent on that peer.
6. Nodes that correctly implement rfc0048 acceptance accept the block; nodes running this code permanently reject it — consensus split.

## Impact Explanation
This is a **Critical** impact: **consensus deviation**. Nodes running this code will permanently blacklist any post-rfc0048 block with a non-zero version field, while correctly-implemented nodes accept it. The `BLOCK_INVALID` status is irrecoverable on the affected peer without a restart and re-sync. A single miner exercising the rfc0048-permitted behavior fragments the network into two incompatible views of the canonical chain.

## Likelihood Explanation
The hardfork activation epoch (9690) has already passed on testnet. Any miner or block producer that sets `header.version = 1` — a behavior explicitly enabled by rfc0048 — will trigger this bug on every peer running this code. No special attacker capability is required beyond normal mining. The bug is a concrete missing call site, not a design ambiguity, and is deterministically reproducible.

## Recommendation
`version_check()` must receive the consensus object and the header's epoch number, then skip the version-zero assertion when the hardfork is active:

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

`HeaderAcceptor` already holds a `HeaderVerifier` that carries a `&Consensus` reference, so threading the consensus object through requires minimal refactoring.

## Proof of Concept
1. Construct a `HeaderView` with `version = 1` and epoch number `>= 9690` (testnet activation).
2. Solve PoW so `PowVerifier` passes inside `non_contextual_check()`.
3. Call `HeaderAcceptor::accept()`.
4. Observe `version_check()` returns `Err(())` and `BlockStatus::BLOCK_INVALID` is inserted for the block hash.
5. Separately call `consensus.hardfork_switch.ckb2023.is_remove_header_version_reservation_rule_enabled(9690)` and observe it returns `true` — proving the predicate exists and is active but is never consulted by `version_check()`.
6. Repeat with a correctly-patched node to confirm the block is accepted, demonstrating the split.