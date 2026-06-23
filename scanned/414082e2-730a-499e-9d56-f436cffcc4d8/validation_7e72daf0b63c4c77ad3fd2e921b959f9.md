### Title
Missing `BLOCK_INVALID` Guard in Header Acceptance Enables Repeated Re-Download of Invalid Blocks — (`sync/src/synchronizer/headers_process.rs`)

---

### Summary

The `HeaderAcceptor::accept()` function in `headers_process.rs` contains an acknowledged `FIXME` comment noting that it fails to return early when a block's status is `BLOCK_INVALID`. Because the only early-exit guard checks for `BLOCK_INVALID`'s absence from the `HEADER_VALID` bitmask (which is a separate bit), a block already known to be invalid can have its header re-accepted, its peer's best-known-header updated, and then be re-queued for download by `block_fetcher.rs`, which also has no `BLOCK_INVALID` guard. This mirrors the external report's pattern: an incorrect/incomplete status check allows an action (retry/re-download) that should be blocked because the outcome is already known.

---

### Finding Description

**Root cause — `headers_process.rs`**

`HeaderAcceptor::accept()` opens with a status check:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    ...
    state.peers().may_set_best_known_header(self.peer, header_index);
    return result;
}
``` [1](#0-0) 

The `BlockStatus` bitflags are defined as:

```
HEADER_VALID  = 1
BLOCK_INVALID = 1 << 12   // completely separate bit
``` [2](#0-1) 

`BLOCK_INVALID` does **not** contain the `HEADER_VALID` bit, so `status.contains(BlockStatus::HEADER_VALID)` returns `false` for an invalid block. The function falls through to the three validation sub-checks (`prev_block_check`, `non_contextual_check`, `version_check`). For a block whose *header* is structurally valid but whose *body* failed contextual verification (e.g., invalid transactions), all three header-level checks pass, and the function reaches:

```rust
sync_shared.insert_valid_header(self.peer, self.header);
``` [3](#0-2) 

This updates the peer's best-known-header to a block the node has already proven invalid.

**Secondary cause — `block_fetcher.rs`**

The block fetcher iterates the best-known-header chain and decides whether to download each block:

```rust
if status.contains(BlockStatus::BLOCK_STORED) {
    ...
    break;
} else if status.contains(BlockStatus::BLOCK_RECEIVED) {
    // Do not download repeatedly
} else if ... && state.write_inflight_blocks().insert(...) {
    fetch.push(header)
}
``` [4](#0-3) 

There is no `BLOCK_INVALID` branch. A block with `BLOCK_INVALID` status matches none of the skip conditions and is unconditionally added to the fetch list.

**`get_block_status` resolution order**

`get_block_status` checks the in-memory `block_status_map` first, then `header_map`, then the database snapshot:

```rust
pub fn get_block_status(&self, block_hash: &Byte32) -> BlockStatus {
    match self.block_status_map().get(block_hash) {
        Some(status_ref) => *status_ref.value(),
        None => {
            if self.header_map().contains_key(block_hash) {
                BlockStatus::HEADER_VALID
            } else { ... }
        }
    }
}
``` [5](#0-4) 

When a block fails contextual verification, the chain service deletes it from the unverified store and writes `BLOCK_INVALID` into `block_status_map`:

```rust
self.delete_unverified_block(&block);
if !is_internal_db_error(err) {
    self.shared.insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
}
``` [6](#0-5) 

Because the block is deleted (not stored), `BLOCK_STORED` is never set. The `block_status_map` entry is the sole source of truth, and it correctly says `BLOCK_INVALID` — but neither `headers_process.rs` nor `block_fetcher.rs` consults it for the purpose of blocking re-processing.

**Attack scenario**

1. An unprivileged peer sends a block whose header satisfies PoW/timestamp/version but whose body contains invalid transactions.
2. The node downloads it, contextual verification fails, the block is deleted, and `BLOCK_INVALID` is written to `block_status_map`.
3. The peer re-sends the header (a `Headers` P2P message).
4. `HeaderAcceptor::accept()` does not return early (FIXME path); all header-level checks pass; `insert_valid_header` is called; the peer's best-known-header is advanced to the invalid block.
5. `BlockFetcher::fetch()` sees the invalid block in the best-known chain, finds no `BLOCK_STORED`/`BLOCK_RECEIVED` match, and enqueues a `GetBlocks` request.
6. The node re-downloads the block, re-runs contextual verification, fails again, and the cycle repeats from step 3.

The attacker pays the PoW cost once; thereafter, repeated header messages (cheap) drive repeated full-block downloads and contextual verification (expensive).

---

### Impact Explanation

Each cycle wastes:
- **Bandwidth**: a full block download per iteration.
- **CPU**: contextual script execution (CKB-VM) per iteration.

A single crafted block can be used to sustain a continuous resource-exhaustion loop against any number of nodes that accept connections from the attacker. During heavy attack, the node's sync throughput degrades and legitimate block processing is delayed.

---

### Likelihood Explanation

- The entry path is a standard P2P `Headers` message, reachable by any unprivised peer.
- The attacker needs one valid-header/invalid-body block (one-time PoW cost).
- The FIXME comment in production code confirms the developers are aware the guard is absent; no special configuration or privileged access is required.
- The `block_fetcher.rs` gap is independent and would also be triggered by any other code path that places an `BLOCK_INVALID` block into the best-known-header chain.

---

### Recommendation

**In `headers_process.rs`**, resolve the FIXME by adding an explicit `BLOCK_INVALID` guard before the `HEADER_VALID` check:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) { ... }
```

**In `block_fetcher.rs`**, add a `BLOCK_INVALID` skip branch alongside the existing `BLOCK_STORED` and `BLOCK_RECEIVED` checks:

```rust
if status.contains(BlockStatus::BLOCK_INVALID) {
    // Already known invalid; do not re-download
} else if status.contains(BlockStatus::BLOCK_STORED) {
    ...
} else if status.contains(BlockStatus::BLOCK_RECEIVED) {
    ...
} else if ... {
    fetch.push(header)
}
```

---

### Proof of Concept

1. Craft block `B` at height `N` with a valid header (correct PoW, timestamp, version, valid parent) but an invalid body (e.g., a transaction spending a non-existent cell).
2. Send `B` to the target node via the block relay protocol. The node downloads it, contextual verification fails, `BLOCK_INVALID` is set.
3. Repeatedly send a `Headers` message containing only `B`'s header.
4. Observe via node metrics that `ckb_inflight_timeout_count` increments and bandwidth is consumed on each cycle, confirming repeated re-download of the same invalid block.

### Citations

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

**File:** sync/src/synchronizer/headers_process.rs (L356-358)
```rust
        sync_shared.insert_valid_header(self.peer, self.header);
        result
    }
```

**File:** shared/src/block_status.rs (L1-17)
```rust
//! Provide BlockStatus
#![allow(missing_docs)]
#![allow(clippy::bad_bit_mask)]

use bitflags::bitflags;
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

**File:** sync/src/synchronizer/block_fetcher.rs (L257-284)
```rust
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

**File:** chain/src/verify.rs (L173-181)
```rust
                self.delete_unverified_block(&block);

                if !is_internal_db_error(err) {
                    self.shared
                        .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
                } else {
                    error!("internal db error, remove block status: {}", block_hash);
                    self.shared.remove_block_status(&block_hash);
                }
```
