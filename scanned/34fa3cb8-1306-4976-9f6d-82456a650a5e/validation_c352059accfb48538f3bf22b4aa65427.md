### Title
Missing Early-Return for `BLOCK_INVALID` Headers Allows Repeated Re-Acceptance of Invalid-Block Headers — (File: `sync/src/synchronizer/headers_process.rs`)

---

### Summary

The `HeaderAcceptor::accept()` function in the sync protocol contains a developer-acknowledged `FIXME` noting that when a header's block status is already `BLOCK_INVALID`, the function should return early but does not. As a result, any unprivileged P2P peer can repeatedly send `SendHeaders` messages containing headers for blocks the local node has already rejected, causing those headers to be re-validated and re-inserted as `HEADER_VALID`. This drives repeated block-download attempts for permanently-invalid blocks, disrupting sync state and wasting CPU and bandwidth.

---

### Finding Description

In `HeaderAcceptor::accept()`, the first status check only short-circuits when the header already carries `HEADER_VALID`:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // ... update peer best-known and return
    return result;
}
```

`BLOCK_INVALID` is a distinct flag from `HEADER_VALID`. A block that failed full contextual verification (e.g., invalid transactions, bad cellbase, script failure) is marked `BLOCK_INVALID` but is **not** marked `HEADER_VALID`. Therefore the early-return is never triggered for such a header, and execution falls through to:

1. `prev_block_check` — passes if the *parent* is not `BLOCK_INVALID`
2. `non_contextual_check` — cryptographic/structural header checks; a block with invalid transactions but a well-formed header passes this
3. `version_check` — passes for version 0

If all three pass, the function calls `sync_shared.insert_valid_header(self.peer, self.header)`, which marks the header `HEADER_VALID` and updates the peer's `best_known_header` to this header.

The cycle is:
1. Attacker sends a block with a valid header but an invalid body (e.g., a transaction that fails script execution).
2. Node verifies the block, marks it `BLOCK_INVALID`.
3. Attacker re-sends the header via `SendHeaders`.
4. `accept()` re-validates the header, passes all three checks, and re-inserts it as `HEADER_VALID`.
5. The sync engine sees the peer's `best_known_header` pointing to this chain and schedules a block download.
6. The block is downloaded, fails verification again, and is marked `BLOCK_INVALID` again.
7. Repeat from step 3 indefinitely. [1](#0-0) [2](#0-1) [3](#0-2) 

---

### Impact Explanation

- **Sync state corruption**: A header for a `BLOCK_INVALID` block is re-stamped `HEADER_VALID`. The peer's `best_known_header` is updated to this header. If the attacker's fabricated chain has higher total difficulty than the honest chain, the node's `shared_best_header` may be updated to point to the invalid chain, causing the node to prefer it during IBD and potentially disconnect honest peers whose work is lower.
- **Repeated wasted work**: Each re-insertion of the header triggers a new block-download request. The block is fetched, fully verified (including CKB-VM script execution), fails, and the cycle restarts. This wastes CPU (script execution), bandwidth (block download), and database I/O.
- **No peer penalty**: The `SendHeaders` handler returns `Status::ok()` after processing valid headers, so the attacker is never banned for this behavior. [4](#0-3) 

---

### Likelihood Explanation

Any unprivileged inbound or outbound P2P peer can send `SendHeaders` messages. The attacker only needs to:
1. Construct one block with a valid header but an invalid body (e.g., a script that always fails — trivial to craft).
2. Relay the block once so the node marks it `BLOCK_INVALID`.
3. Continuously re-send the header.

No special privileges, no majority hashpower, and no Sybil attack are required. A single peer connection is sufficient.

---

### Recommendation

Add an early return at the top of `HeaderAcceptor::accept()` for the `BLOCK_INVALID` case, returning a `ValidationState::Invalid` result (analogous to the `InvalidParent` path):

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) {
    // ... existing early-return path
}
```

This resolves the FIXME and prevents re-processing of headers whose blocks are already known-invalid. [1](#0-0) 

---

### Proof of Concept

1. Connect to a CKB node as a peer.
2. Craft a block `B` with:
   - A valid header (correct PoW, valid parent hash, valid timestamp, version 0).
   - An invalid body (e.g., a transaction whose lock script always returns failure).
3. Send `B` via `SendBlock`. The node verifies it, fails at script execution, and sets `BlockStatus::BLOCK_INVALID` for `B`'s hash.
4. In a loop, send `SendHeaders([B.header])` to the node.
5. Observe (via metrics or logs) that:
   - Each iteration re-inserts `B`'s header as `HEADER_VALID` (`insert_valid_header` is called).
   - The sync engine schedules a `GetBlocks` request for `B`.
   - The node re-downloads and re-verifies `B`, failing again.
   - CPU usage and block-download bandwidth increase proportionally to the loop rate. [5](#0-4)

### Citations

**File:** sync/src/synchronizer/headers_process.rs (L154-179)
```rust
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

**File:** sync/src/synchronizer/headers_process.rs (L295-357)
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
```
