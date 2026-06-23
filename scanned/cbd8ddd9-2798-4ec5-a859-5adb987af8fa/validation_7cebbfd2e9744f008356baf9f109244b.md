### Title
Missing `BLOCK_INVALID` Status Short-Circuit in `HeaderAcceptor::accept()` Allows Repeated Re-processing of Previously-Rejected Headers — (File: `sync/src/synchronizer/headers_process.rs`)

---

### Summary

The `HeaderAcceptor::accept()` function checks whether a header already has `HEADER_VALID` status and returns early, but contains no corresponding early-return for `BLOCK_INVALID` status. A developer-acknowledged `FIXME` comment in the code explicitly marks this gap. As a result, any unprivileged P2P peer can repeatedly re-submit previously-rejected headers and force the node to re-execute all header validation logic on each submission.

---

### Finding Description

In `sync/src/synchronizer/headers_process.rs`, the `HeaderAcceptor::accept()` function (line 295) retrieves the current block status at line 303 and immediately short-circuits at line 304 if `HEADER_VALID` is set:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // ... update peer state and return
    return result;
}
``` [1](#0-0) 

The `BLOCK_INVALID` flag is a separate bit (`1 << 12`) in the `BlockStatus` bitfield: [2](#0-1) 

`BLOCK_INVALID` is set in three places within `accept()` itself — after `prev_block_check` fails (line 330), after `non_contextual_check` fails (line 341), and after `version_check` fails (line 352): [3](#0-2) 

Despite this, there is no guard at the top of `accept()` that checks `status.contains(BlockStatus::BLOCK_INVALID)` and returns early. The `FIXME` comment at lines 301–302 is a developer acknowledgment that this check is intentionally missing only because the correct error type to return was undecided — not because the check is unnecessary.

When a peer re-sends a header that was previously rejected and marked `BLOCK_INVALID`, the node will:
1. Retrieve the status (which includes `BLOCK_INVALID`)
2. Skip the `HEADER_VALID` branch (correct)
3. **Proceed to re-run `prev_block_check`, `non_contextual_check`, and `version_check`** — all of which will fail again for the same deterministic reasons
4. Re-mark the header `BLOCK_INVALID` and return

This is the direct analog of the Aragon Voting bug: a state check that should gate further processing is absent, allowing an actor to re-trigger work on an already-rejected object.

`HeadersProcess::execute()` processes up to `MAX_HEADERS_LEN` (2000) headers per `SendHeaders` P2P message: [4](#0-3) 

Each of those 2000 headers, if previously marked `BLOCK_INVALID`, will be fully re-validated instead of short-circuited.

---

### Impact Explanation

An unprivileged P2P peer can craft a `SendHeaders` message containing up to 2000 headers that are deterministically invalid (e.g., wrong version, invalid PoW, invalid parent chain). After the first submission the node marks all of them `BLOCK_INVALID`. On every subsequent re-submission the node re-runs `HeaderVerifier::verify` (which includes PoW verification and median-time checks) for each header, rather than short-circuiting on the cached `BLOCK_INVALID` status. This is a CPU-based resource exhaustion vector reachable from any peer without any privilege.

---

### Likelihood Explanation

Medium. Any peer on the P2P network can send `SendHeaders` messages. The attack requires no special privilege, no key material, and no majority hashpower. The attacker only needs to maintain a connection and repeatedly send the same invalid header batch. The `FIXME` comment confirms the developers are aware the guard is absent.

---

### Recommendation

Add an explicit `BLOCK_INVALID` early-return at the top of `HeaderAcceptor::accept()`, immediately after the `HEADER_VALID` check. The appropriate result is `ValidationState::Invalid`. For example:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::BLOCK_INVALID) {
    state.invalid(None);   // already known-bad; no need to re-validate
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) {
    // ... existing logic
    return result;
}
```

This resolves the acknowledged `FIXME` and ensures that previously-rejected headers are rejected in O(1) rather than re-running full validation.

---

### Proof of Concept

1. Connect to a CKB node as an unprivileged peer via the sync protocol.
2. Send a `SendHeaders` P2P message containing 2000 headers with an invalid version field (`version != 0`). The node will run `version_check`, fail, and call `shared.insert_block_status(hash, BlockStatus::BLOCK_INVALID)` for each header.
3. Immediately re-send the identical `SendHeaders` message.
4. Observe (via CPU profiling or logging) that `HeaderVerifier::verify` is invoked again for all 2000 headers, rather than being short-circuited by the cached `BLOCK_INVALID` status.
5. Repeat in a tight loop to sustain elevated CPU consumption on the victim node.

The root cause is confirmed at: [5](#0-4)

### Citations

**File:** sync/src/synchronizer/headers_process.rs (L106-109)
```rust
        if headers.len() > MAX_HEADERS_LEN {
            warn!("HeadersProcess is oversized");
            return StatusCode::HeadersIsInvalid.with_context("oversize");
        }
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

**File:** sync/src/synchronizer/headers_process.rs (L324-354)
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
```

**File:** shared/src/block_status.rs (L9-17)
```rust
        const UNKNOWN                 =     0;

        const HEADER_VALID            =     1;
        const BLOCK_RECEIVED          =     1 | (Self::HEADER_VALID.bits() << 1);
        const BLOCK_STORED            =     1 | (Self::BLOCK_RECEIVED.bits() << 1);
        const BLOCK_VALID             =     1 | (Self::BLOCK_STORED.bits() << 1);

        const BLOCK_INVALID           =     1 << 12;
    }
```
