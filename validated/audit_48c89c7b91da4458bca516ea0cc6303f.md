Audit Report

## Title
`HeaderAcceptor::accept()` Bypasses `BLOCK_INVALID` Guard, Inserting Known-Invalid Headers as Valid - (File: `sync/src/synchronizer/headers_process.rs`)

## Summary
`HeaderAcceptor::accept()` contains an acknowledged `FIXME` where a header whose block was previously marked `BLOCK_INVALID` is not rejected early. Because `BLOCK_INVALID` (`1 << 12 = 4096`) does not share any bits with `HEADER_VALID` (`1`), the early-return guard at line 304 is bypassed. The function then re-runs only lightweight non-contextual checks, which pass for blocks invalidated for contextual reasons, and proceeds to call `insert_valid_header` at line 356, inserting the invalid block's header into `header_map` and corrupting the peer's `best_known_header` and potentially the global `shared_best_header`.

## Finding Description
In `sync/src/synchronizer/headers_process.rs`, `HeaderAcceptor::accept()` at lines 301–304 contains an explicit `FIXME` comment acknowledging the missing guard:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) { ... }
``` [1](#0-0) 

`BlockStatus::BLOCK_INVALID = 1 << 12 = 4096` and `BlockStatus::HEADER_VALID = 1` are confirmed in `shared/src/block_status.rs`: [2](#0-1) 

The bitwise check `4096 & 1 == 0` means `status.contains(HEADER_VALID)` is `false` for `BLOCK_INVALID` blocks, so the early-return is skipped entirely.

The code then falls through to three checks:
1. `prev_block_check` (lines 244–253) — only checks whether the *parent* is `BLOCK_INVALID`, not the header itself. [3](#0-2) 
2. `non_contextual_check` (lines 255–283) — runs `HeaderVerifier` (PoW nonce, timestamp, epoch, version). [4](#0-3) 
3. `version_check` (lines 286–293) — checks `header.version() == 0`. [5](#0-4) 

A block marked `BLOCK_INVALID` due to contextual body failures (invalid transactions, wrong cellbase reward, invalid DAO header, etc.) has a valid header that passes all three checks. The function then reaches line 356:

```rust
sync_shared.insert_valid_header(self.peer, self.header);
``` [6](#0-5) 

`insert_valid_header` (lines 1094–1141 of `sync/src/types/mod.rs`) inserts the header into `header_map`, calls `may_set_best_known_header` to update the peer's chain tip, and calls `may_set_shared_best_header` to potentially update the global best header — all for a block the node already knows is invalid. [7](#0-6) 

By contrast, `CompactBlockProcess` at lines 259–260 of `sync/src/relayer/compact_block_process.rs` correctly guards:

```rust
} else if status.contains(BlockStatus::BLOCK_INVALID) {
    return StatusCode::BlockIsInvalid.with_context(block_hash);
}
``` [8](#0-7) 

The `SendHeaders` path has no equivalent guard.

## Impact Explanation
Once `best_known_header` is set to a known-invalid block, `BlockFetcher` (lines 159–169 of `sync/src/synchronizer/block_fetcher.rs`) uses it to determine which blocks to download. [9](#0-8) 

The fetcher loop (lines 247–284) checks `BLOCK_STORED` and `BLOCK_RECEIVED` but does **not** check `BLOCK_INVALID` when iterating candidates, so it issues `GetBlocks` requests for blocks on a chain the node already knows is invalid, wasting bandwidth and CPU. [10](#0-9) 

If `shared_best_header` is also corrupted (when the invalid block has higher total difficulty), the node's IBD state machine and sync scheduling are disrupted, potentially stalling legitimate sync. This maps to the **High** impact class: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."

## Likelihood Explanation
The attacker must first cause a block to be marked `BLOCK_INVALID` for contextual reasons — this requires mining a block with valid PoW but an invalid body (e.g., wrong cellbase reward). On mainnet this is expensive (requires real hash power), making the precondition non-trivial. However, the cost is one-time: once such a block exists, the attacker can repeatedly send its header via `SendHeaders` to any number of nodes at negligible cost, amplifying the impact. The `FIXME` comment in the source code confirms the developers are aware of the gap. The entry path (`SendHeaders` P2P message → `HeadersProcess::execute()` → `HeaderAcceptor::accept()`) requires no privilege.

## Recommendation
Add an explicit `BLOCK_INVALID` guard at the top of `HeaderAcceptor::accept()`, before the `HEADER_VALID` check, mirroring what `CompactBlockProcess` already does:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) {
    // existing path
}
```

## Proof of Concept
1. Connect a malicious peer to a CKB node.
2. Mine a block whose header is valid (correct PoW, timestamp, epoch) but whose body is contextually invalid (e.g., cellbase output exceeds the allowed reward). Relay it to the target node. The node processes it, fails contextual verification, and sets `block_status_map[block_hash] = BLOCK_INVALID`.
3. Send a `SendHeaders` P2P message containing that same block's header.
4. `HeadersProcess::execute()` calls `HeaderAcceptor::accept()`.
5. `get_block_status` returns `BLOCK_INVALID` (= `4096`). `status.contains(HEADER_VALID)` = `(4096 & 1) != 0` = `false`. The early-return is skipped.
6. `prev_block_check` passes (the parent is valid). `non_contextual_check` passes (the header's PoW/timestamp/epoch are valid). `version_check` passes.
7. `insert_valid_header` is called: the invalid block's header is inserted into `header_map`, and `best_known_header` for the peer is updated to the invalid block.
8. Observe via `get_peers` RPC that `best_known_header_hash` now points to the known-invalid block. `BlockFetcher` subsequently issues `GetBlocks` requests for blocks on the invalid chain.

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

**File:** sync/src/synchronizer/headers_process.rs (L255-283)
```rust
    pub fn non_contextual_check(&self, state: &mut ValidationResult) -> Result<(), bool> {
        self.verifier.verify(self.header).map_err(|error| {
            debug!(
                "HeadersProcess accepted {:?} error {:?}",
                self.header.number(),
                error
            );
            // UnknownParentError surfaces as BlockError(UnknownParent), not
            // HeaderError.  Missing parent is a recoverable ordering/context
            // issue, not proof that this header is invalid.
            if error
                .downcast_ref::<BlockError>()
                .is_some_and(|e| e.kind() == BlockErrorKind::UnknownParent)
            {
                state.temporary_invalid(Some(ValidationError::Verify(error)));
                false
            } else if let Some(header_error) = error.downcast_ref::<HeaderError>() {
                if header_error.is_too_new() {
                    state.temporary_invalid(Some(ValidationError::Verify(error)));
                    false
                } else {
                    state.invalid(Some(ValidationError::Verify(error)));
                    true
                }
            } else {
                state.invalid(Some(ValidationError::Verify(error)));
                true
            }
        })
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

**File:** sync/src/synchronizer/headers_process.rs (L301-304)
```rust
        // FIXME If status == BLOCK_INVALID then return early. But which error
        // type should we return?
        let status = self.active_chain.get_block_status(&self.header.hash());
        if status.contains(BlockStatus::HEADER_VALID) {
```

**File:** sync/src/synchronizer/headers_process.rs (L356-357)
```rust
        sync_shared.insert_valid_header(self.peer, self.header);
        result
```

**File:** shared/src/block_status.rs (L11-16)
```rust
        const HEADER_VALID            =     1;
        const BLOCK_RECEIVED          =     1 | (Self::HEADER_VALID.bits() << 1);
        const BLOCK_STORED            =     1 | (Self::BLOCK_RECEIVED.bits() << 1);
        const BLOCK_VALID             =     1 | (Self::BLOCK_STORED.bits() << 1);

        const BLOCK_INVALID           =     1 << 12;
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

**File:** sync/src/relayer/compact_block_process.rs (L259-261)
```rust
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```

**File:** sync/src/synchronizer/block_fetcher.rs (L159-169)
```rust
        let best_known = match self.peer_best_known_header() {
            Some(t) => t,
            None => {
                debug!(
                    "Peer {} doesn't have best known header; ignore it",
                    self.peer
                );
                return None;
            }
        };
        if !best_known.is_better_than(self.active_chain.total_difficulty()) {
```

**File:** sync/src/synchronizer/block_fetcher.rs (L247-284)
```rust
            let mut status = self
                .sync_shared
                .active_chain()
                .get_block_status(&header.hash());

            // Judge whether we should fetch the target block, neither stored nor in-flighted
            for _ in 0..span {
                let parent_hash = header.parent_hash();
                let hash = header.hash();

                if status.contains(BlockStatus::BLOCK_STORED) {
                    if status.contains(BlockStatus::BLOCK_VALID) {
                        // If the block is stored, its ancestor must on store
                        // So we can skip the search of this space directly
                        self.sync_shared
                            .state()
                            .peers()
                            .set_last_common_header(self.peer, header.number_and_hash());
                    }

                    end = window_end(header.number(), BLOCK_DOWNLOAD_WINDOW, best_known.number());
                    break;
                } else if status.contains(BlockStatus::BLOCK_RECEIVED) {
                    // Do not download repeatedly
                } else if (matches!(self.ibd, IBDState::In)
                    || state.compare_with_pending_compact(&hash, now))
                    && state
                        .write_inflight_blocks()
                        .insert(self.peer, (header.number(), hash).into())
                {
                    debug!(
                        "block: {}-{} added to inflight, block_status: {:?}",
                        header.number(),
                        header.hash(),
                        status
                    );
                    fetch.push(header)
                }
```
