### Title
Missing `BLOCK_INVALID` Early-Exit Guard in Header Sync Acceptor Allows Repeated Re-Processing of Known-Invalid Headers — (`File: sync/src/synchronizer/headers_process.rs`)

---

### Summary

`HeaderAcceptor::accept()` contains an acknowledged-but-unimplemented guard: a `FIXME` comment explicitly states that headers with `BLOCK_INVALID` status should cause an early return, but the code never performs that check. Any unprivileged P2P peer can repeatedly send `SendHeaders` messages containing headers already marked `BLOCK_INVALID`, forcing the node to re-run all header verification work and, in the worst case, overwrite a `BLOCK_INVALID` status with `HEADER_VALID`.

---

### Finding Description

In `HeaderAcceptor::accept()`, the very first thing the function does is read the current block status: [1](#0-0) 

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    ...
    return result;   // ← only HEADER_VALID causes an early exit
}
```

The guard for `HEADER_VALID` is implemented; the guard for `BLOCK_INVALID` is explicitly noted as missing. After this block, the function unconditionally proceeds through three further checks:

1. `prev_block_check` — rejects only if the **parent** is `BLOCK_INVALID`, not the header itself.
2. `non_contextual_check` — re-runs PoW, timestamp, epoch, and number verification.
3. `version_check` — re-checks the version field. [2](#0-1) 

If all three pass, `insert_valid_header` is called unconditionally:

```rust
sync_shared.insert_valid_header(self.peer, self.header);
```

A header can be marked `BLOCK_INVALID` by the full block verifier (e.g., invalid transactions, bad reward, bad DAO accounting) even though its header-level fields (PoW, timestamp, epoch, version) are individually valid. When such a header is re-submitted via `SendHeaders`, all three checks above will pass, and `insert_valid_header` will be called — potentially overwriting the `BLOCK_INVALID` status with `HEADER_VALID`.

The `execute()` loop in `HeadersProcess` processes up to `MAX_HEADERS_LEN` (2000) headers per message: [3](#0-2) 

Each header triggers the full verification pipeline with no short-circuit for previously-rejected headers.

---

### Impact Explanation

**Two distinct impacts:**

1. **CPU exhaustion (DoS):** An attacker repeatedly sends `SendHeaders` messages containing up to 2000 headers that are already `BLOCK_INVALID`. Each message causes the node to re-run PoW verification, median-time computation, epoch checks, and number checks for every header, with no cache or early exit. This is reachable from any unauthenticated P2P peer.

2. **Status corruption / re-download loop:** For headers whose `BLOCK_INVALID` status was set by the full block verifier (not by header-level checks), the header-level checks in `accept()` will pass, and `insert_valid_header` will be called. This can overwrite the `BLOCK_INVALID` status with `HEADER_VALID`, causing the sync layer to re-request the full block from peers and re-attempt verification — an unbounded re-download loop for a block the node already knows is invalid.

---

### Likelihood Explanation

**Medium.** The `SendHeaders` message is a standard, unauthenticated P2P sync message processed from any connected peer. No special privilege is required. An attacker only needs to:
1. Submit a block that passes header checks but fails full block verification (e.g., a block with a valid PoW but an invalid transaction or reward).
2. Wait for the node to mark it `BLOCK_INVALID`.
3. Repeatedly send `SendHeaders` containing that header's data.

This is a realistic scenario for a malicious peer on the public network.

---

### Recommendation

Implement the missing guard immediately after reading `status`:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());

// Implement the FIXME: reject known-invalid headers immediately.
if status.contains(BlockStatus::BLOCK_INVALID) {
    state.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
    return result;
}

if status.contains(BlockStatus::HEADER_VALID) {
    ...
}
```

This mirrors the existing `prev_block_check` logic that already rejects headers whose **parent** is `BLOCK_INVALID`, and closes the gap acknowledged by the `FIXME` comment.

---

### Proof of Concept

1. Connect a malicious peer to a CKB node.
2. Mine a block with a valid header (valid PoW, timestamp, epoch) but an invalid body (e.g., a transaction that spends a non-existent cell). Submit it via `submit_block` RPC or relay it via the P2P block relay protocol.
3. The node's full block verifier marks the block hash as `BLOCK_INVALID` in shared state.
4. The malicious peer sends a `SendHeaders` P2P message containing that block's header (and up to 1999 other such headers).
5. `HeadersProcess::execute()` calls `HeaderAcceptor::accept()` for each header.
6. Because `status` is `BLOCK_INVALID` (not `HEADER_VALID`), the early-exit at line 304 is **not** taken.
7. `prev_block_check`, `non_contextual_check`, and `version_check` all pass (the header itself is well-formed).
8. `insert_valid_header` is called, overwriting `BLOCK_INVALID` with `HEADER_VALID`.
9. The sync layer now considers the block a candidate for download and re-verification.
10. Repeat from step 4 indefinitely. [4](#0-3)

### Citations

**File:** sync/src/synchronizer/headers_process.rs (L106-109)
```rust
        if headers.len() > MAX_HEADERS_LEN {
            warn!("HeadersProcess is oversized");
            return StatusCode::HeadersIsInvalid.with_context("oversize");
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
