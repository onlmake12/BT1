Audit Report

## Title
Missing `BLOCK_INVALID` Early-Return in `HeaderAcceptor::accept` Allows Sync State Corruption via Replayed Invalid Headers — (`File: sync/src/synchronizer/headers_process.rs`)

## Summary

`HeaderAcceptor::accept()` contains a developer-acknowledged `FIXME` noting that a header with `BLOCK_INVALID` status is never rejected early. Because `BLOCK_INVALID` (bit 12) and `HEADER_VALID` (bit 0) occupy disjoint bits, the only early-exit guard (`status.contains(HEADER_VALID)`) never fires for invalid blocks. Any connected peer can replay a previously-rejected block's header via `SendHeaders`, pass all three sub-checks, and cause `insert_valid_header` to insert the header into `header_map` and corrupt the peer's best-known header and `shared_best_header`.

## Finding Description

**Root cause — `sync/src/synchronizer/headers_process.rs`, `HeaderAcceptor::accept`, lines 301–322:**

The FIXME comment at line 301 explicitly acknowledges the missing guard:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) { ... return result; }
``` [1](#0-0) 

`BlockStatus` is a bitflag type where `HEADER_VALID = 1` and `BLOCK_INVALID = 1 << 12 = 4096`. These bits are disjoint, so `BLOCK_INVALID.contains(HEADER_VALID)` is always `false`. [2](#0-1) 

This is confirmed by the existing test suite, which explicitly asserts that `BLOCK_INVALID` does not contain `HEADER_VALID`: [3](#0-2) 

**Code path after the missing guard:**

1. `prev_block_check` (lines 244–253): checks only the *parent* block's status, not the header itself. A block can be `BLOCK_INVALID` because its body (transactions) failed verification while its header and parent are perfectly valid. [4](#0-3) 

2. `non_contextual_check` (lines 255–283): runs `HeaderVerifier` (PoW, timestamp, etc.). A block marked `BLOCK_INVALID` due to body failure has a cryptographically valid header that passes this check.

3. `version_check` (lines 286–293): trivially passes for any version-0 header.

4. `sync_shared.insert_valid_header(self.peer, self.header)` at line 356 is called unconditionally. [5](#0-4) 

**What `insert_valid_header` does:**

`insert_valid_header` inserts the header into `header_map`, updates the peer's best-known header via `may_set_best_known_header`, and calls `may_set_shared_best_header`. [6](#0-5) 

Critically, `insert_valid_header` does **not** call `insert_block_status`, so `block_status_map` retains `BLOCK_INVALID`. However, `get_block_status` checks `block_status_map` first, then falls back to `header_map`. This means the `BLOCK_INVALID` entry in `block_status_map` is not overwritten, but the `header_map` is now polluted and the peer/shared best-known header state is corrupted. [7](#0-6) 

**Downstream effects of corrupted sync state:**

`asynchronous_process_remote_block` in `sync/src/synchronizer/mod.rs` gates block body processing on `status.contains(HEADER_VALID)`. Since `block_status_map` still holds `BLOCK_INVALID`, the block body won't be re-processed. However, the corrupted peer best-known header and `shared_best_header` cause the node to issue `GetBlocks` requests for blocks along the invalid chain, wasting bandwidth and CPU on re-download attempts that will silently fail. [8](#0-7) 

**Contrast with `compact_block_process.rs`:**

The relay path has the correct guard at lines 259–260:

```rust
} else if status.contains(BlockStatus::BLOCK_INVALID) {
    return StatusCode::BlockIsInvalid.with_context(block_hash);
}
``` [9](#0-8) 

The sync header path is inconsistent with the relay path.

## Impact Explanation

The corrupted `shared_best_header` and per-peer best-known header cause the node to continuously issue `GetBlocks` requests for blocks along a chain it has already determined to be invalid. An attacker with multiple peers can keep all of them pointing to an invalid chain, causing sustained bandwidth and CPU waste on the victim node and on any peers that respond to the spurious `GetBlocks` requests. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, since the attack is cheap (send a single `SendHeaders` message per peer), repeatable, and requires no special privilege.

## Likelihood Explanation

Any connected P2P peer can send `SendHeaders` messages. The `BLOCK_INVALID` condition arises naturally whenever a block body fails verification (invalid transaction, script failure, etc.). The attacker only needs to know the hash of any previously-rejected block — information that is observable from ordinary block relay. The FIXME comment is an explicit developer acknowledgment that the guard is missing. No key material, majority hash power, or victim mistake is required. The attack is repeatable for the duration of the session.

## Recommendation

Add an explicit early-return for `BLOCK_INVALID` immediately before the `HEADER_VALID` check in `HeaderAcceptor::accept`, mirroring the guard already present in `compact_block_process.rs`:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());

if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(None);
    return result;
}

if status.contains(BlockStatus::HEADER_VALID) {
    // ... existing path
}
```

## Proof of Concept

1. Run a CKB node in dev mode.
2. Submit a block whose body fails verification (e.g., invalid script). The chain service sets `BLOCK_INVALID` in `block_status_map` for that block hash.
3. From a second peer, send a `packed::SendHeaders` P2P message containing only that block's header (which has valid PoW for dev mode).
4. Observe that `HeaderAcceptor::accept` does not return early: `status.contains(HEADER_VALID)` is `false` for `BLOCK_INVALID`, `prev_block_check` passes (parent is valid), `non_contextual_check` passes (header is cryptographically valid), `version_check` passes.
5. `insert_valid_header` is called: the header is inserted into `header_map` and the peer's best-known header is updated to the previously-rejected block.
6. Confirm: `block_status_map` still holds `BLOCK_INVALID`, but `header_map` now contains the header and the peer's best-known header points to the invalid block.
7. Observe the node issuing `GetBlocks` requests for the invalid block, which silently fail at `asynchronous_process_remote_block` (status is not `HEADER_VALID` from `block_status_map`), creating a persistent sync disruption.

### Citations

**File:** sync/src/synchronizer/headers_process.rs (L244-253)
```rust
    pub fn prev_block_check(&self, state: &mut ValidationResult) -> Result<(), ()> {
        if self.active_chain.contains_block_status(
            &self.header.data().raw().parent_hash(),
            BlockStatus::BLOCK_INVALID,
        ) {
            state.invalid(Some(ValidationError::InvalidParent));
            return Err(());
        }
        Ok(())
    }
```

**File:** sync/src/synchronizer/headers_process.rs (L301-322)
```rust
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
```

**File:** sync/src/synchronizer/headers_process.rs (L356-357)
```rust
        sync_shared.insert_valid_header(self.peer, self.header);
        result
```

**File:** shared/src/block_status.rs (L6-17)
```rust
bitflags! {
    #[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
    pub struct BlockStatus: u32 {
        const UNKNOWN                 =     0;

        const HEADER_VALID            =     1;
        const BLOCK_RECEIVED          =     1 | (Self::HEADER_VALID.bits() << 1);
        const BLOCK_STORED            =     1 | (Self::BLOCK_RECEIVED.bits() << 1);
        const BLOCK_VALID             =     1 | (Self::BLOCK_STORED.bits() << 1);

        const BLOCK_INVALID           =     1 << 12;
    }
```

**File:** sync/src/tests/block_status.rs (L82-86)
```rust
fn test_block_invalid() {
    let target = BlockStatus::BLOCK_INVALID;
    let includes = vec![BlockStatus::BLOCK_INVALID];
    assert_contain(includes, target);
}
```

**File:** sync/src/types/mod.rs (L1129-1141)
```rust
        self.shared.header_map().insert(header_view.clone());
        self.state
            .peers()
            .may_set_best_known_header(peer, header_view.as_header_index());
        if header_view.number().is_multiple_of(10000) {
            info!(
                "inserted valid header: header {}-{}",
                header_view.number(),
                header_view.hash()
            );
        }
        self.state.may_set_shared_best_header(header_view);
    }
```

**File:** shared/src/shared.rs (L425-445)
```rust
    pub fn get_block_status(&self, block_hash: &Byte32) -> BlockStatus {
        match self.block_status_map().get(block_hash) {
            Some(status_ref) => *status_ref.value(),
            None => {
                if self.header_map().contains_key(block_hash) {
                    BlockStatus::HEADER_VALID
                } else {
                    let verified = self
                        .snapshot()
                        .get_block_ext(block_hash)
                        .map(|block_ext| block_ext.verified);
                    match verified {
                        None => BlockStatus::UNKNOWN,
                        Some(None) => BlockStatus::BLOCK_STORED,
                        Some(Some(true)) => BlockStatus::BLOCK_VALID,
                        Some(Some(false)) => BlockStatus::BLOCK_INVALID,
                    }
                }
            }
        }
    }
```

**File:** sync/src/synchronizer/mod.rs (L470-486)
```rust
    pub fn asynchronous_process_remote_block(&self, remote_block: RemoteBlock) {
        let block_hash = remote_block.block.hash();
        let status = self.shared.active_chain().get_block_status(&block_hash);
        // NOTE: Filtering `BLOCK_STORED` but not `BLOCK_RECEIVED`, is for avoiding
        // stopping synchronization even when orphan_pool maintains dirty items by bugs.
        if status.contains(BlockStatus::BLOCK_STORED) {
            error!("Block {} already stored", block_hash);
        } else if status.contains(BlockStatus::HEADER_VALID) {
            self.shared.accept_remote_block(&self.chain, remote_block);
        } else {
            debug!(
                "Synchronizer process_new_block unexpected status {:?} {}",
                status, block_hash,
            );
            // TODO which error should we return?
        }
    }
```

**File:** sync/src/relayer/compact_block_process.rs (L259-261)
```rust
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```
