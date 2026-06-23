### Title
`BLOCK_INVALID` Status Fetched But Not Checked Before Re-processing in `HeaderAcceptor::accept()` — (`sync/src/synchronizer/headers_process.rs`)

---

### Summary

In `HeaderAcceptor::accept()`, the block status is fetched via `get_block_status()` and used to short-circuit only the `HEADER_VALID` case. The `BLOCK_INVALID` flag — though fetched — is never checked before falling through to re-run all header verification steps (including expensive PoW verification). A developer `FIXME` comment in the code explicitly acknowledges this gap. An unprivileged P2P peer can exploit this by repeatedly sending `SendHeaders` messages containing headers already marked `BLOCK_INVALID`, forcing the node to re-execute all verification checks on each delivery.

---

### Finding Description

In `sync/src/synchronizer/headers_process.rs`, the `HeaderAcceptor::accept()` function fetches the block status and checks for `HEADER_VALID`:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // update peer state and return early
    return result;
}
``` [1](#0-0) 

The `BLOCK_INVALID` flag is fetched as part of `status` but is never tested. The code falls through unconditionally to:

1. `prev_block_check` — checks if the parent is `BLOCK_INVALID`
2. `non_contextual_check` — runs `HeaderVerifier::verify()`, which includes PoW verification
3. `version_check` [2](#0-1) 

The `BLOCK_INVALID` status is defined in `shared/src/block_status.rs`: [3](#0-2) 

This status is set in multiple places — when a parent is invalid, when non-contextual verification fails, or when version is wrong — all within `accept()` itself. Once set, it is never consulted on re-entry.

The analog to the Astaria bug is exact:

| Astaria | CKB |
|---|---|
| `isShutdown` fetched from vault state | `BLOCK_INVALID` fetched via `get_block_status()` |
| Flag not checked before `commitToLien` | Flag not checked before re-running all header checks |
| Vault shutdown bypassed | Known-invalid header re-verified |

The developer `FIXME` comment at line 301–302 explicitly acknowledges the missing guard.

---

### Impact Explanation

A malicious peer can:

1. Send a `SendHeaders` message with a header containing invalid PoW (or an invalid parent, or wrong version).
2. The node marks the header `BLOCK_INVALID` after the first verification.
3. The peer repeatedly re-sends the same `SendHeaders` message.
4. On each delivery, `accept()` fetches `BLOCK_INVALID` status, ignores it, and re-runs `non_contextual_check` — which calls `HeaderVerifier::verify()` including PoW verification.

PoW verification (Eaglesong) is computationally non-trivial. A single peer sending a tight loop of `SendHeaders` messages with a pre-crafted invalid header forces the victim node to re-verify PoW on every message, consuming CPU proportional to the peer's send rate. There is no early-exit guard to prevent this re-work.

---

### Likelihood Explanation

- **Entry path**: Any unauthenticated P2P peer can send `SendHeaders` messages. No special role, key, or privilege is required.
- **Triggering condition**: The peer only needs to send a header that fails any of the three checks (invalid PoW, invalid parent, wrong version). The first delivery marks it `BLOCK_INVALID`; every subsequent delivery re-triggers the full check path.
- **No rate-limit specific to this path**: The `accept()` function is called per-header in the `HeadersProcess::execute()` loop with no per-hash deduplication guard for the `BLOCK_INVALID` case. [4](#0-3) 

---

### Recommendation

Add an explicit early-return for `BLOCK_INVALID` immediately after the `HEADER_VALID` check in `HeaderAcceptor::accept()`:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // ... existing logic
    return result;
}
// Add this guard:
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(None); // or a dedicated ValidationError variant
    return result;
}
```

This mirrors the pattern already used in `compact_block_process.rs` where `BLOCK_INVALID` is checked and causes an immediate return: [5](#0-4) 

---

### Proof of Concept

1. Connect to a CKB node as an unprivileged P2P peer using the sync protocol.
2. Craft a `SendHeaders` message containing a single header with a valid structure but invalid PoW (e.g., nonce that does not satisfy the Eaglesong target).
3. Send the message once. The node runs `non_contextual_check` → `HeaderVerifier::verify()` → PoW fails → `insert_block_status(hash, BLOCK_INVALID)`.
4. In a tight loop, re-send the identical `SendHeaders` message.
5. On each iteration, `accept()` fetches `BLOCK_INVALID` from the status map, skips the `HEADER_VALID` branch, and falls through to re-run `HeaderVerifier::verify()` (PoW check) again.
6. Observe sustained CPU consumption on the victim node proportional to the send rate, with no corresponding useful work performed.

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

**File:** shared/src/block_status.rs (L1-18)
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
}
```

**File:** sync/src/relayer/compact_block_process.rs (L259-261)
```rust
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```
