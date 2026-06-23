### Title
Missing `BLOCK_INVALID` Status Check in Batch Header Acceptance Allows Re-acceptance of Previously-Invalidated Headers - (File: `sync/src/synchronizer/headers_process.rs`)

### Summary
`HeaderAcceptor::accept()` in `sync/src/synchronizer/headers_process.rs` contains an acknowledged but unresolved FIXME: when a header's block status is `BLOCK_INVALID`, the function does not return early. Instead it falls through all non-contextual checks. If the header itself is structurally valid (only the block body was invalid), all checks pass and `insert_valid_header` is called, overriding `BLOCK_INVALID` with `HEADER_VALID`. This allows any unprivileged peer to repeatedly re-trigger block fetching and re-processing of previously-invalidated blocks via crafted `SendHeaders` messages.

### Finding Description
`HeadersProcess::execute()` processes a peer-supplied batch of headers. For each header, it delegates to `HeaderAcceptor::accept()`.

Inside `accept()`:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // ... update best known header and return
    return result;
}
// Falls through to prev_block_check, non_contextual_check, version_check
``` [1](#0-0) 

The code correctly short-circuits when `HEADER_VALID`, but there is **no corresponding early return for `BLOCK_INVALID`**. The three checks that follow are all non-contextual (parent-invalid check, header PoW/structure verifier, version check): [2](#0-1) 

A header can be marked `BLOCK_INVALID` for contextual reasons — e.g., the block body failed reward verification, DAO withdrawal verification, or script execution — while the header itself is structurally sound. In that case all three non-contextual checks pass, and `insert_valid_header` is called: [3](#0-2) 

This overwrites the `BLOCK_INVALID` status with `HEADER_VALID`, causing the sync state machine to schedule a fresh `GetBlocks` request for the same block. When the block arrives and is re-verified, it is marked `BLOCK_INVALID` again — and the cycle can be repeated indefinitely by the attacker.

The batch entry point `HeadersProcess::execute()` checks size, emptiness, and chain continuity, but has no guard against headers whose hashes are already in `BLOCK_INVALID` state: [4](#0-3) 

### Impact Explanation
- **Block status map corruption**: `BLOCK_INVALID` is transiently overwritten with `HEADER_VALID`, causing the node to treat a known-bad block as pending validation.
- **Repeated block download and re-verification**: The node fetches and fully re-verifies the same invalid block on every iteration of the attack, wasting CPU and bandwidth.
- **Sync state machine interference**: Inflight-block tracking, peer scoring, and best-known-header state are all updated based on the spuriously re-accepted header, potentially disrupting normal sync progress.

### Likelihood Explanation
Any unprivileged peer that can establish a connection can send `SendHeaders` messages. The attacker first sends one invalid block (valid header, invalid body — e.g., a block with a bad script execution result or invalid cellbase reward) to cause the node to mark that hash `BLOCK_INVALID`. Thereafter, the attacker repeatedly sends `SendHeaders` containing that header. No special privilege, key material, or majority hashpower is required. The attack is cheap to sustain and the node has no rate-limit on `SendHeaders` processing beyond the `MAX_HEADERS_LEN` count check.

### Recommendation
Add an explicit early-return guard for `BLOCK_INVALID` in `HeaderAcceptor::accept()`, resolving the existing FIXME:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::BLOCK_INVALID) {
    state.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) {
    // ... existing logic
    return result;
}
```

### Proof of Concept
1. Connect to a target CKB node as an unprivileged peer.
2. Craft a block with a valid header (valid PoW, valid timestamp, valid version) but an invalid body (e.g., script execution failure or invalid cellbase output capacity).
3. Send it via `SendBlock`. The node verifies the block, fails contextual/script checks, and marks the block hash as `BLOCK_INVALID`.
4. Construct a `SendHeaders` message containing only that header (one entry, passes `MAX_HEADERS_LEN` and `is_continuous`).
5. Send the `SendHeaders` message. `HeaderAcceptor::accept()` does not return early for `BLOCK_INVALID`; all non-contextual checks pass; `insert_valid_header` is called; the node schedules a `GetBlocks` request.
6. Respond with the same invalid block. The node re-verifies, re-marks `BLOCK_INVALID`.
7. Return to step 4 — the loop repeats, continuously consuming the node's verification resources.

### Citations

**File:** sync/src/synchronizer/headers_process.rs (L106-130)
```rust
        if headers.len() > MAX_HEADERS_LEN {
            warn!("HeadersProcess is oversized");
            return StatusCode::HeadersIsInvalid.with_context("oversize");
        }

        if headers.is_empty() {
            // Empty means that the other peer's tip may be consistent with our own best known,
            // but empty cannot 100% confirm this, so it does not set the other peer's best header
            // to the shared best known.
            // This action means that if the newly connected node has not been sync with headers,
            // it cannot be used as a synchronization node.
            debug!("HeadersProcess is_empty (synchronized)");
            if let Some(mut state) = self.synchronizer.peers().state.get_mut(&self.peer) {
                self.synchronizer
                    .shared()
                    .state()
                    .tip_synced(state.value_mut());
            }
            return Status::ok();
        }

        if !self.is_continuous(&headers) {
            warn!("HeadersProcess is not continuous");
            return StatusCode::HeadersIsInvalid.with_context("not continuous");
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

**File:** sync/src/synchronizer/headers_process.rs (L324-358)
```rust
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
