### Title
Missing `BLOCK_INVALID` Guard in `HeaderAcceptor::accept()` Allows Re-acceptance of Previously-Rejected Headers — (`File: sync/src/synchronizer/headers_process.rs`)

### Summary
`HeaderAcceptor::accept()` contains a developer-acknowledged `FIXME` comment noting that a `BLOCK_INVALID` status check is missing before processing an incoming header. Because the guard is absent, a header for a block that was previously fully verified and marked `BLOCK_INVALID` can be re-accepted as valid, re-inserted into the header map, and used to update a peer's best-known header — potentially triggering repeated re-download of a known-invalid block.

### Finding Description

`BlockStatus::BLOCK_INVALID` is a well-defined sentinel value in the shared block-status map, set whenever a block fails any stage of verification (non-contextual, contextual, or full script execution). [1](#0-0) 

The `HeaderAcceptor::accept()` function in the sync layer is responsible for deciding whether an incoming header from a peer should be accepted and inserted into the header map. At the top of `accept()`, the code reads the current block status and explicitly notes — via a `FIXME` comment — that a `BLOCK_INVALID` early-return is needed but not implemented: [2](#0-1) 

The code only short-circuits on `HEADER_VALID`. If the status is `BLOCK_INVALID` (bit `1 << 12`), it does **not** overlap with `HEADER_VALID` (bit `1`), so the early-return is skipped. Execution then falls through to `prev_block_check`, `non_contextual_check`, and `version_check`: [3](#0-2) 

These three checks are header-level only. A block can be marked `BLOCK_INVALID` during full contextual verification (e.g., invalid transactions, script failure) while its header still passes all three header-level checks. In that case, `sync_shared.insert_valid_header(self.peer, self.header)` is called at line 356, re-inserting the previously-invalidated block's header as valid and updating the peer's best-known header.

This is the direct analog of the WeirollWallet bug: the `BLOCK_INVALID` flag exists precisely to gate this operation, but the check is absent — acknowledged by the `FIXME` comment at lines 301–302.

### Impact Explanation

A malicious peer can repeatedly send `SendHeaders` messages containing a header for a block that the local node has already fully verified and rejected (marked `BLOCK_INVALID`). Each time, `accept()` will re-accept the header, re-insert it into the header map, and update the peer's best-known header. This can cause the sync subsystem to repeatedly schedule download of the same invalid block, wasting bandwidth, CPU (re-verification), and memory. The node never permanently ignores the invalid header as it should.

### Likelihood Explanation

Any unprivileged remote peer can send `SendHeaders` messages. The attacker only needs to know the hash of a block that the target node has previously rejected — which can be learned by observing the network or by deliberately crafting a block that passes header checks but fails full verification. No keys, privileges, or majority hashpower are required.

### Recommendation

Add an explicit `BLOCK_INVALID` guard at the top of `accept()`, before the `HEADER_VALID` check, resolving the existing `FIXME`:

```rust
pub fn accept(&self) -> ValidationResult {
    let mut result = ValidationResult::default();
    let sync_shared = self.active_chain.sync_shared();
    let state = self.active_chain.state();
    let shared = sync_shared.shared();

    let status = self.active_chain.get_block_status(&self.header.hash());

    // NEW: reject headers for blocks already known to be invalid
    if status.contains(BlockStatus::BLOCK_INVALID) {
        result.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
        return result;
    }

    if status.contains(BlockStatus::HEADER_VALID) {
        // ... existing logic ...
    }
    // ... rest of function ...
}
```

### Proof of Concept

1. Node A receives a compact block from peer P. The block's header passes `HeaderVerifier` but the block body contains an invalid transaction. Full verification in `ConsumeUnverifiedBlockProcessor::consume_unverified_blocks` fails; the block hash is written to `block_status_map` as `BLOCK_INVALID`. [4](#0-3) 

2. Peer P (or any peer) sends a `SendHeaders` message containing the same header.

3. `HeadersProcess::execute()` calls `HeaderAcceptor::accept()` for that header. [5](#0-4) 

4. Inside `accept()`, `get_block_status` returns `BLOCK_INVALID`. The `HEADER_VALID` branch is not taken (bits don't overlap). No `BLOCK_INVALID` guard exists. Execution continues. [6](#0-5) 

5. `prev_block_check` passes (parent is valid). `non_contextual_check` passes (header PoW/structure is fine). `version_check` passes.

6. `sync_shared.insert_valid_header(self.peer, self.header)` is called at line 356, re-inserting the invalid block's header as `HEADER_VALID` and updating the peer's best-known header — undoing the `BLOCK_INVALID` marking for sync purposes.

7. The sync subsystem may now schedule re-download of the same invalid block. Steps 1–7 can be repeated indefinitely by the attacker.

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

**File:** sync/src/synchronizer/headers_process.rs (L154-158)
```rust
        for header in headers.iter().skip(1) {
            let verifier = HeaderVerifier::new(shared, consensus);
            let acceptor =
                HeaderAcceptor::new(header, self.peer, verifier, self.active_chain.clone());
            let result = acceptor.accept();
```

**File:** sync/src/synchronizer/headers_process.rs (L295-322)
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

**File:** chain/src/verify.rs (L175-178)
```rust
                if !is_internal_db_error(err) {
                    self.shared
                        .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
                } else {
```
