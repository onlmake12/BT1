### Title
Missing `BLOCK_INVALID` Status Check in Header Acceptance Allows Repeated Re-validation DoS — (`sync/src/synchronizer/headers_process.rs`)

---

### Summary

The `HeaderAcceptor::accept()` function in the synchronizer checks whether a header already has `HEADER_VALID` status and returns early, but it does **not** check whether the header has already been marked `BLOCK_INVALID`. This is even acknowledged with a `// FIXME` comment in the code. As a result, an unprivileged peer can repeatedly send headers that the node has already permanently rejected, forcing the node to re-run the full validation suite on each submission instead of returning early, enabling a CPU-exhaustion denial-of-service attack.

---

### Finding Description

In `sync/src/synchronizer/headers_process.rs`, the `accept()` method performs the following status check before validation:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // ... update peer state and return early
    return result;
}
```

The code correctly short-circuits when a header is already known-valid (`HEADER_VALID`). However, it **does not** short-circuit when the header is already known-invalid (`BLOCK_INVALID`). The `BlockStatus` flags are defined as a bitflag hierarchy:

```rust
pub struct BlockStatus: u32 {
    const UNKNOWN          =     0;
    const HEADER_VALID     =     1;
    const BLOCK_RECEIVED   =     1 | (Self::HEADER_VALID.bits() << 1);
    const BLOCK_STORED     =     1 | (Self::BLOCK_RECEIVED.bits() << 1);
    const BLOCK_VALID      =     1 | (Self::BLOCK_STORED.bits() << 1);
    const BLOCK_INVALID    =     1 << 12;
}
```

`BLOCK_INVALID` is a disjoint flag — a header marked `BLOCK_INVALID` will **not** satisfy `status.contains(BlockStatus::HEADER_VALID)`, so the early-return is never triggered for invalid headers.

After the missing check, the code proceeds to run three full validation passes:

1. `prev_block_check` — verifies the parent block is not invalid
2. `non_contextual_check` — verifies PoW, timestamps, difficulty, etc.
3. `version_check` — verifies the header version field

Each of these is non-trivial (PoW verification in particular is computationally expensive). If all three pass, `insert_valid_header` is called — meaning a header previously marked `BLOCK_INVALID` could, in theory, be re-accepted as `HEADER_VALID` if the conditions that caused the original rejection no longer apply (e.g., the parent was later resolved). More critically, for headers that are **permanently** invalid (e.g., bad PoW, wrong version), the node re-runs all checks on every submission before re-marking them `BLOCK_INVALID`, with no rate-limiting benefit from the status cache.

The analog to the external report is direct:

| External Report (`setPosMode`) | CKB (`HeaderAcceptor::accept`) |
|---|---|
| Checks `newModeStatus.canBorrow` ✓ | Checks `HEADER_VALID` ✓ |
| Checks `currentModeStatus.canRepay` ✓ | — |
| **Missing: `newModeStatus.canRepay`** ✗ | **Missing: `BLOCK_INVALID` early exit** ✗ |
| Allows bypassing liquidation | Allows bypassing validation cache |

---

### Impact Explanation

A malicious peer can send `SendHeaders` messages containing headers that the local node has already permanently rejected and cached as `BLOCK_INVALID`. Because the early-exit for `BLOCK_INVALID` is absent, the node re-executes the full validation pipeline — including PoW verification — for every such header on every submission. By repeatedly sending batches of crafted invalid headers, an attacker can cause sustained CPU exhaustion on the target node, degrading or halting its ability to process legitimate blocks and headers from honest peers.

---

### Likelihood Explanation

Any unprivileged peer can send `SendHeaders` messages. The attacker only needs to:
1. Establish a peer connection (no authentication required).
2. Send headers that were previously rejected (or craft new ones with invalid PoW that are cheap to generate but expensive to verify).
3. Repeat indefinitely.

The `// FIXME` comment confirms the developers are aware of the missing check, indicating it is a real gap and not intentional behavior. The attack requires no special privileges, no key material, and no majority hashpower.

---

### Recommendation

Add an explicit `BLOCK_INVALID` early-return at the top of `accept()`, immediately after (or before) the `HEADER_VALID` check:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // already accepted — update peer state and return
    // ...
    return result;
}
// Add this:
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(Some(ValidationError::InvalidParent)); // or a dedicated error variant
    return result;
}
```

This mirrors the pattern already used in `compact_block_process.rs` (`contextual_check`), which correctly checks `BLOCK_INVALID` before proceeding:

```rust
} else if status.contains(BlockStatus::BLOCK_INVALID) {
    return StatusCode::BlockIsInvalid.with_context(block_hash);
}
```

---

### Proof of Concept

1. Attacker connects to a CKB node as a peer.
2. Attacker sends a `SendHeaders` message containing a header `H` with invalid PoW (or any other permanently-invalid property). The node validates it, marks it `BLOCK_INVALID`, and stores that status in `block_status_map`.
3. Attacker immediately re-sends the same header `H` in another `SendHeaders` message.
4. The node's `accept()` checks `status.contains(BlockStatus::HEADER_VALID)` → false (it's `BLOCK_INVALID`, not `HEADER_VALID`). The missing `BLOCK_INVALID` check means no early exit occurs.
5. The node re-runs `prev_block_check`, `non_contextual_check` (including PoW), and `version_check` in full.
6. Steps 3–5 repeat indefinitely, consuming CPU proportional to the rate of re-submission.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** sync/src/relayer/compact_block_process.rs (L256-261)
```rust
    } else if status.contains(BlockStatus::BLOCK_RECEIVED) {
        // block already in orphan pool
        return Status::ignored();
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```
