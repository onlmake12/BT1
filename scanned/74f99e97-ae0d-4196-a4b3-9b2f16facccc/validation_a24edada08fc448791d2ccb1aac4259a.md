### Title
Missing `BLOCK_INVALID` Status Check in `HeadersProcess::accept()` Allows Repeated Re-Processing of Known-Invalid Headers - (File: sync/src/synchronizer/headers_process.rs)

---

### Summary

The `accept()` function in `sync/src/synchronizer/headers_process.rs` checks whether a header has `HEADER_VALID` status but explicitly skips checking for `BLOCK_INVALID` status (acknowledged by a `FIXME` comment in the code). As a result, any unprivileged P2P peer can repeatedly relay headers that were previously marked `BLOCK_INVALID`, forcing the node to re-execute the full validation pipeline (`prev_block_check`, `non_contextual_check`, `version_check`) on each delivery. The analogous check is correctly present in `compact_block_process.rs`, making this an inconsistent omission in the headers path.

---

### Finding Description

In `sync/src/synchronizer/headers_process.rs`, the `accept()` function begins by fetching the block status:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // update peer state and return early
    return result;
}
``` [1](#0-0) 

The code returns early only when `HEADER_VALID` is set. When the status is `BLOCK_INVALID`, the function falls through and continues executing `prev_block_check`, `non_contextual_check`, and `version_check` — the full validation pipeline — before eventually re-marking the header invalid.

The `BlockStatus` flags are defined as:

```rust
const BLOCK_INVALID = 1 << 12;
``` [2](#0-1) 

By contrast, the compact block handler in `sync/src/relayer/compact_block_process.rs` correctly guards against this case:

```rust
} else if status.contains(BlockStatus::BLOCK_INVALID) {
    return StatusCode::BlockIsInvalid.with_context(block_hash);
}
``` [3](#0-2) 

The missing guard in `headers_process.rs` means the node will re-run all header validation work for every `SendHeaders` message that contains a previously-rejected header, with no rate limit beyond the peer connection itself.

---

### Impact Explanation

An unprivileged P2P peer can craft a `SendHeaders` message containing one or more headers that the local node has already marked `BLOCK_INVALID`. Because the guard is absent, the node re-executes `prev_block_check`, `non_contextual_check` (which includes PoW verification), and `version_check` for each such header on every delivery. A single persistent peer repeatedly sending the same invalid header can sustain elevated CPU load on the victim node. At scale (multiple peers, many invalid headers per message), this constitutes a targeted resource-exhaustion / DoS vector against the sync subsystem. The node's ability to process legitimate headers from honest peers is degraded.

---

### Likelihood Explanation

The attack requires only a standard P2P connection — no keys, no stake, no privileged role. The attacker needs to have previously observed or induced a header to be marked `BLOCK_INVALID` on the target node (e.g., by sending a malformed header once), then replay it indefinitely. The `FIXME` comment in the source confirms the developers are aware the check is missing, indicating this is a known gap rather than an intentional design choice.

---

### Recommendation

Add an early-return guard for `BLOCK_INVALID` immediately after fetching the block status, mirroring the pattern already used in `compact_block_process.rs`:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(None); // or a dedicated ValidationError variant
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) {
    // existing early-return logic
    return result;
}
``` [1](#0-0) 

---

### Proof of Concept

1. Attacker connects to a victim CKB node as a standard P2P peer.
2. Attacker sends a `SendHeaders` message containing a single syntactically valid but semantically invalid header (e.g., wrong parent hash). The node processes it, fails `prev_block_check`, and stores `BLOCK_INVALID` for that header hash.
3. Attacker enters a tight loop, repeatedly sending `SendHeaders` messages containing the same `BLOCK_INVALID` header.
4. On each delivery, `HeadersProcess::accept()` fetches the status, finds it is not `HEADER_VALID`, and falls through to re-execute `prev_block_check`, `non_contextual_check` (PoW check), and `version_check`.
5. The victim node's sync thread burns CPU re-validating a header it has already permanently rejected, with no mechanism to short-circuit the work.

The `FIXME` comment at line 301 of `sync/src/synchronizer/headers_process.rs` is the direct root-cause evidence. [4](#0-3)

### Citations

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

**File:** sync/src/relayer/compact_block_process.rs (L259-261)
```rust
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```
