### Title
Missing `BLOCK_INVALID` State Guard in `HeaderAcceptor::accept()` Allows Re-validation of Previously-Rejected Headers — (File: `sync/src/synchronizer/headers_process.rs`)

---

### Summary

`HeaderAcceptor::accept()` checks for `HEADER_VALID` status and returns early, but contains an explicit `FIXME` acknowledging it does **not** check for `BLOCK_INVALID` status before proceeding with full header re-validation. This is a direct structural analog to the reported Solidity bug: a function that should branch on entity state but only handles one state, silently falling through for another. A malicious peer can repeatedly relay headers for blocks already marked `BLOCK_INVALID`, causing the node to re-run all header validation checks and potentially re-insert those headers as `HEADER_VALID`, creating a state inconsistency and a CPU-exhaustion vector.

---

### Finding Description

In `sync/src/synchronizer/headers_process.rs`, `HeaderAcceptor::accept()` reads the current block status and branches only on `HEADER_VALID`:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // update peer state and return
    return result;
}
// Falls through for BLOCK_INVALID — no early return
``` [1](#0-0) 

The three subsequent checks — `prev_block_check`, `non_contextual_check`, and `version_check` — do **not** inspect the header's own stored status. They only check the **parent's** invalidity, the header's PoW/timestamp/epoch fields, and the version field: [2](#0-1) 

If a block was marked `BLOCK_INVALID` during **full block validation** (e.g., script execution failure, capacity overflow, reward mismatch — none of which are re-checked here), its header can still pass all three checks in `accept()`. The function then calls `sync_shared.insert_valid_header(self.peer, self.header)`, re-inserting the header as `HEADER_VALID`: [3](#0-2) 

This creates a state where the same block hash carries both `BLOCK_INVALID` (from prior full-block rejection) and `HEADER_VALID` (from re-validation), which is an inconsistency. It also updates the peer's best-known-header to this block, potentially triggering further block download requests for a block the node already knows is invalid.

The `BLOCK_INVALID` status is set in multiple places during full block processing: [4](#0-3) [5](#0-4) 

The missing branch is the exact analog of the Solidity bug: the function should be:

```rust
if status.contains(BlockStatus::BLOCK_INVALID) {
    state.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) {
    // update peer state and return
    return result;
}
```

---

### Impact Explanation

1. **State inconsistency**: A block hash can simultaneously hold `BLOCK_INVALID` and `HEADER_VALID` bits. Any downstream code that checks `HEADER_VALID` without also checking `BLOCK_INVALID` may treat a known-bad block as having a valid header, affecting sync decisions and peer scoring.

2. **Redundant resource consumption / DoS**: A malicious peer can repeatedly send the same previously-rejected header. Each delivery triggers `prev_block_check` + `non_contextual_check` (which runs the full header verifier including PoW verification) + `version_check` + `insert_valid_header`. This is unbounded CPU work per peer message for headers the node already knows are invalid.

3. **Incorrect peer best-known-header**: `may_set_best_known_header` is called with the re-validated header, skewing the sync peer selection logic toward a peer advertising a chain that the local node has already determined is invalid.

---

### Likelihood Explanation

Any unprivileged P2P peer can send `SendHeaders` or `SendBlock` messages containing arbitrary headers. The `HeadersProcess` handler is reachable from any connected peer without authentication. The attacker only needs to know the hash of a block that was previously rejected by the target node (e.g., by observing a prior relay attempt that was rejected). This is a low-effort, high-repeatability attack. [6](#0-5) 

---

### Recommendation

Add an explicit `BLOCK_INVALID` guard at the top of `HeaderAcceptor::accept()`, before any validation work is performed:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());

if status.contains(BlockStatus::BLOCK_INVALID) {
    state.invalid(Some(ValidationError::InvalidParent)); // reuse or add a dedicated variant
    return result;
}

if status.contains(BlockStatus::HEADER_VALID) {
    // existing early-return logic
    ...
    return result;
}
```

This mirrors the fix recommended in the external report: check the entity's current state first, then dispatch to the appropriate code path.

---

### Proof of Concept

1. Node A receives and fully validates a block `B` whose transactions fail script execution. Node A sets `BlockStatus::BLOCK_INVALID` for `B`'s hash.
2. Malicious peer P connects to Node A and sends a `SendHeaders` message containing the header of block `B`.
3. `HeadersProcess::execute()` calls `HeaderAcceptor::new(...).accept()`.
4. `accept()` reads `status = BLOCK_INVALID`. Since `BLOCK_INVALID` does not contain `HEADER_VALID`, the early-return branch is skipped.
5. `prev_block_check` passes (B's parent is valid).
6. `non_contextual_check` passes (B's header PoW, timestamp, epoch are all valid — only the body was invalid).
7. `version_check` passes (version == 0).
8. `insert_valid_header` is called, setting `HEADER_VALID` for B's hash and updating P's best-known-header to B.
9. P can repeat this indefinitely, each time forcing Node A to re-run PoW verification and re-update sync state for a known-invalid block. [7](#0-6)

### Citations

**File:** sync/src/synchronizer/headers_process.rs (L127-179)
```rust
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

        for header in headers.iter().skip(1) {
            let verifier = HeaderVerifier::new(shared, consensus);
            let acceptor =
                HeaderAcceptor::new(header, self.peer, verifier, self.active_chain.clone());
            let result = acceptor.accept();
            match result.state {
                ValidationState::Invalid => {
                    debug!(
                        "HeadersProcess accept result is invalid, error = {:?}, header = {:?}",
                        result.error, headers,
                    );
                    return StatusCode::HeadersIsInvalid
                        .with_context(format!("accept header {header:?}"));
                }
                ValidationState::TemporaryInvalid => {
                    debug!(
                        "HeadersProcess accept result is temporarily invalid, header = {:?}",
                        header
                    );
                    return Status::ok();
                }
                ValidationState::Valid => {
                    // Valid, do nothing
                }
            };
        }
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
