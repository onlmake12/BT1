### Title
Missing `BLOCK_INVALID` Early-Return in Header Sync State Machine Allows Repeated Re-Processing of Known-Invalid Headers — (`sync/src/synchronizer/headers_process.rs`)

---

### Summary

`HeaderAcceptor::accept()` in the sync protocol's header validation state machine has a developer-acknowledged `FIXME` noting that it should return early when a header's status is already `BLOCK_INVALID`, but it does not. Instead, the code falls through and re-runs all validation checks as if the header were unknown. This is the CKB analog of the tBTC bug: one branch of the state machine (the `BLOCK_INVALID` early-return path) is effectively unreachable/missing, causing all `BLOCK_INVALID` headers to be processed through the wrong code path.

---

### Finding Description

`BlockStatus` is a bitflag state machine with the following states: [1](#0-0) 

`HeaderAcceptor::accept()` is supposed to handle three cases for an incoming header:
1. Already known-valid (`HEADER_VALID`): return early, update peer's best-known header.
2. Already known-invalid (`BLOCK_INVALID`): return early with an error.
3. Unknown: run all checks.

The code correctly handles case 1, but case 2 is missing — acknowledged by a `FIXME` comment: [2](#0-1) 

Because `BLOCK_INVALID = 1 << 12` does not contain the `HEADER_VALID` bit (`= 1`), the `status.contains(BlockStatus::HEADER_VALID)` guard is false for `BLOCK_INVALID` headers. The code then falls through to `prev_block_check`, `non_contextual_check`, and `version_check` — re-running all validation work — and if all checks pass, calls `insert_valid_header`: [3](#0-2) 

The three check functions are: [4](#0-3) 

---

### Impact Explanation

**1. Resource exhaustion (DoS):** An unprivileged P2P peer can repeatedly relay `SendHeaders` messages containing headers already marked `BLOCK_INVALID`. Each delivery causes the node to re-run `HeaderVerifier::verify()`, `prev_block_check`, and `version_check` for every such header, wasting CPU proportional to the number of headers per message (up to `MAX_HEADERS_LEN = 2000`).

**2. Missing peer ban:** The correct behavior for a peer sending a known-invalid header is to immediately return an error and ban the peer. Because the early-return is missing, the node instead re-runs checks and returns `StatusCode::HeadersIsInvalid` only after the re-run fails. Whether this triggers a ban depends on the `should_ban()` implementation for that status code — but the peer is never identified as sending a *known-bad* header.

**3. Potential state confusion (edge case):** If a header was marked `BLOCK_INVALID` because its parent was invalid at the time, but the `block_status_map` entry for the parent is later absent (e.g., after a node restart clears the in-memory map and the parent was never persisted to the store), `prev_block_check` would pass. If `non_contextual_check` and `version_check` also pass, `insert_valid_header` is called, adding the previously-rejected header to the `header_map` as `HEADER_VALID`. The `get_block_status` function checks `block_status_map` first, so the status would remain `BLOCK_INVALID` if the entry is still present — but if it was cleared, the header would be treated as valid. [5](#0-4) 

---

### Likelihood Explanation

Any unprivileged P2P peer can send `SendHeaders` messages. The sync protocol processes them in `HeadersProcess::execute()`: [6](#0-5) 

An attacker only needs to know (or guess) the hash of a header that the target node has previously rejected. This information can be inferred from public chain data or by probing the node. The attack requires no special privileges, no majority hashpower, and no Sybil capability.

---

### Recommendation

Add an explicit early-return for `BLOCK_INVALID` at the top of `HeaderAcceptor::accept()`, resolving the `FIXME`:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) {
    // ... existing early-return logic
}
```

This ensures that peers sending known-invalid headers are immediately identified and banned, and that no redundant computation is performed.

---

### Proof of Concept

1. Connect to a CKB node as a P2P peer using the Sync protocol.
2. Send a `SendHeaders` message containing a header `H` whose hash is already in the node's `block_status_map` as `BLOCK_INVALID` (e.g., a header with an invalid parent, previously rejected by the node).
3. Observe that the node re-runs `HeaderVerifier::verify()`, `prev_block_check`, and `version_check` for `H` instead of returning immediately.
4. Repeat step 2 in a tight loop. The node's CPU usage increases proportionally, with no ban applied to the sending peer (since the ban, if any, is triggered only after re-running the checks, not on the basis of the cached `BLOCK_INVALID` status).

The `FIXME` comment at line 301 of `sync/src/synchronizer/headers_process.rs` is the developers' own acknowledgment that this path is missing. [7](#0-6)

### Citations

**File:** shared/src/block_status.rs (L8-17)
```rust
    pub struct BlockStatus: u32 {
        const UNKNOWN                 =     0;

        const HEADER_VALID            =     1;
        const BLOCK_RECEIVED          =     1 | (Self::HEADER_VALID.bits() << 1);
        const BLOCK_STORED            =     1 | (Self::BLOCK_RECEIVED.bits() << 1);
        const BLOCK_VALID             =     1 | (Self::BLOCK_STORED.bits() << 1);

        const BLOCK_INVALID           =     1 << 12;
    }
```

**File:** sync/src/synchronizer/headers_process.rs (L94-152)
```rust
    pub fn execute(self) -> Status {
        debug!("HeadersProcess begins");
        let shared: &SyncShared = self.synchronizer.shared();
        let consensus = shared.consensus();
        let headers = self
            .message
            .headers()
            .to_entity()
            .into_iter()
            .map(packed::Header::into_view)
            .collect::<Vec<_>>();

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

        let result = self.accept_first(&headers[0]);
        match result.state {
            ValidationState::Invalid => {
                debug!(
                    "HeadersProcess accept_first result is invalid, error = {:?}, first header = {:?}",
                    result.error, headers[0]
                );
                return StatusCode::HeadersIsInvalid
                    .with_context(format!("accept first header {:?}", headers[0]));
            }
            ValidationState::TemporaryInvalid => {
                debug!(
                    "HeadersProcess accept_first result is temporary invalid, first header = {:?}",
                    headers[0]
                );
                return Status::ok();
            }
            ValidationState::Valid => {
                // Valid, do nothing
            }
        };
```

**File:** sync/src/synchronizer/headers_process.rs (L244-293)
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
    }

    pub fn version_check(&self, state: &mut ValidationResult) -> Result<(), ()> {
        if self.header.version() != 0 {
            state.invalid(Some(ValidationError::Version));
            Err(())
        } else {
            Ok(())
        }
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

**File:** sync/src/synchronizer/headers_process.rs (L324-357)
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
