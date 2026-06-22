### Title
Missing `BLOCK_INVALID` Status Check in `HeaderAcceptor::accept()` Allows Repeated Re-processing of Previously-Rejected Headers — (File: `sync/src/synchronizer/headers_process.rs`)

---

### Summary

`HeaderAcceptor::accept()` in the sync module checks whether a header's block status contains `HEADER_VALID` to skip re-processing, but it never checks for `BLOCK_INVALID`. When a header has been previously marked `BLOCK_INVALID` (e.g., after contextual verification failure), the function falls through all validation steps and may call `insert_valid_header`, inserting the invalid block into the `header_map` and updating the node's `shared_best_header`. This is a developer-acknowledged bug (a `FIXME` comment is present at the exact location). Any unprivileged P2P peer can exploit this by repeatedly relaying headers for previously-rejected blocks, causing the node to waste CPU re-validating them, corrupt the shared best header state, and potentially re-request and re-process the full block.

---

### Finding Description

In `HeaderAcceptor::accept()`, the early-exit guard reads:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // ... update peer best-known header and return
    return result;
}
``` [1](#0-0) 

The `BlockStatus` flags are defined as a cumulative bitfield:

```
HEADER_VALID  = 1          (bit 0)
BLOCK_RECEIVED= 3          (bits 0-1)
BLOCK_STORED  = 7          (bits 0-2)
BLOCK_VALID   = 15         (bits 0-3)
BLOCK_INVALID = 4096       (bit 12, isolated)
``` [2](#0-1) 

Because `BLOCK_INVALID = 4096` has no overlap with `HEADER_VALID = 1`, the expression `BlockStatus::BLOCK_INVALID.contains(BlockStatus::HEADER_VALID)` evaluates to **false** (`4096 & 1 = 0`). The early-return guard is therefore never triggered for `BLOCK_INVALID` headers.

The function then proceeds through:

1. `prev_block_check` — only rejects if the *parent* is `BLOCK_INVALID`; a block whose parent is valid but whose own full-block contextual verification previously failed will pass this check.
2. `non_contextual_check` — only verifies header syntax and PoW; a block that failed script execution or reward accounting will pass this check.
3. `version_check` — trivially passes for any version-0 header.
4. **`sync_shared.insert_valid_header(self.peer, self.header)`** — inserts the header into `header_map` and calls `may_set_shared_best_header`, potentially advancing the node's view of the best chain to include a block that was already verified and found invalid. [3](#0-2) 

`get_block_status` consults `block_status_map` before `header_map`, so the block's persisted `BLOCK_INVALID` entry is not overwritten. However, `insert_valid_header` still inserts a stale `HeaderIndexView` into `header_map` and calls `may_set_shared_best_header`: [4](#0-3) 

If the invalid block's total difficulty exceeds the current `shared_best_header`, the node's sync anchor is corrupted to point at an invalid chain tip.

---

### Impact Explanation

An unprivileged P2P peer can send a `Headers` message containing a header for a block that was previously contextually rejected (e.g., failed script execution). Because `HeaderAcceptor::accept()` does not guard against `BLOCK_INVALID`, the node:

1. Re-runs non-contextual header validation (CPU waste, repeatable at will).
2. Calls `insert_valid_header`, inserting the invalid block into `header_map`.
3. Potentially advances `shared_best_header` to a chain tip that includes the invalid block.
4. May trigger downstream block-fetch logic to re-request and re-process the full block (bandwidth and CPU waste).

The `shared_best_header` corruption affects the node's fork-choice and sync scheduling, causing it to chase a chain it cannot actually commit.

---

### Likelihood Explanation

The entry path is the standard P2P `Headers` message, reachable by any connected peer with no authentication or privilege. The attacker only needs to know the hash of a block that the target node previously rejected during contextual verification (e.g., a block with a failing lock script). Such hashes are observable on-chain or can be crafted locally. The attack is repeatable and stateless from the attacker's perspective.

---

### Recommendation

Add an explicit `BLOCK_INVALID` guard immediately after the existing status check, resolving the `FIXME`:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::BLOCK_INVALID) {
    state.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) {
    // ... existing early-return path
    return result;
}
```

This mirrors the pattern already used in `prev_block_check` and `compact_block_process::contextual_check`, which both explicitly test for `BLOCK_INVALID` before proceeding. [5](#0-4) [6](#0-5) 

---

### Proof of Concept

1. Attacker connects to a CKB node as a standard P2P peer.
2. Attacker crafts or observes a block `B` whose header passes PoW and non-contextual checks but whose scripts fail contextual verification. The node processes `B`, marks it `BLOCK_INVALID` in `block_status_map`.
3. Attacker sends a `Headers` message containing the header of `B` to the same node.
4. `HeadersProcess::execute()` calls `HeaderAcceptor::accept()` for `B`'s header.
5. `get_block_status` returns `BLOCK_INVALID` (4096). `4096 & 1 = 0`, so the `HEADER_VALID` guard does not fire.
6. `prev_block_check` passes (parent of `B` is valid).
7. `non_contextual_check` passes (header syntax and PoW are valid).
8. `version_check` passes (version = 0).
9. `insert_valid_header` is called: `B`'s header is inserted into `header_map`; `may_set_shared_best_header` is invoked.
10. If `B`'s total difficulty exceeds the current `shared_best_header`, the node's sync anchor is now pointing at an invalid chain tip.
11. Steps 3–10 can be repeated indefinitely by the attacker. [7](#0-6) [8](#0-7)

### Citations

**File:** sync/src/synchronizer/headers_process.rs (L244-252)
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
