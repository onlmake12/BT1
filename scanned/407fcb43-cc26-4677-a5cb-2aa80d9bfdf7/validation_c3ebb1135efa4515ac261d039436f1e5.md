### Title
Missing `BLOCK_INVALID` Early-Exit in `HeaderAcceptor::accept()` Allows Repeated Re-Processing of Rejected Headers — (`File: sync/src/synchronizer/headers_process.rs`)

---

### Summary

`HeaderAcceptor::accept()` checks for `BLOCK_INVALID` status with a `// FIXME` comment but never actually guards against it. A header already permanently rejected and stored as `BLOCK_INVALID` in `block_status_map` is not short-circuited; instead it falls through all validation sub-checks (including PoW/`HeaderVerifier::verify()`). An unprivileged remote peer can repeatedly send `SendHeaders` P2P messages containing previously-rejected headers, forcing the node to re-run expensive cryptographic validation for each one.

---

### Finding Description

`BlockStatus` is a bitflag type with the following values: [1](#0-0) 

`BLOCK_INVALID` is `1 << 12 = 4096`. It shares **no bits** with `HEADER_VALID` (value `1`), `BLOCK_RECEIVED`, `BLOCK_STORED`, or `BLOCK_VALID`.

In `HeaderAcceptor::accept()`, the only early-exit status check is:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // ... update peer best known and return
    return result;
}
``` [2](#0-1) 

Because `BLOCK_INVALID` does not contain the `HEADER_VALID` bit, a header whose hash is already stored as `BLOCK_INVALID` in `block_status_map` is **not caught** by this guard. The function then proceeds through:

1. `prev_block_check` — checks if the *parent* is `BLOCK_INVALID` (not the header itself)
2. `non_contextual_check` — calls `HeaderVerifier::verify()`, which includes PoW (Eaglesong) and timestamp checks
3. `version_check`
4. On success: `sync_shared.insert_valid_header(self.peer, self.header)` — inserts into `header_map` and updates `may_set_best_known_header` / `may_set_shared_best_header` [3](#0-2) 

The `get_block_status` function checks `block_status_map` before `header_map`, so the `BLOCK_INVALID` status persists for subsequent lookups: [4](#0-3) 

However, `insert_valid_header` still executes, inserting the header into `header_map` and calling `may_set_best_known_header` and `may_set_shared_best_header` with the invalid header's index: [5](#0-4) 

The `FIXME` comment is the codebase's own acknowledgment that this guard is missing.

The `HeadersProcess::execute()` loop processes up to `MAX_HEADERS_LEN` (2000) headers per message: [6](#0-5) 

---

### Impact Explanation

**Primary — CPU exhaustion / DoS:** A malicious peer sends `SendHeaders` messages containing up to 2000 headers already marked `BLOCK_INVALID`. Each header triggers a full `HeaderVerifier::verify()` call (Eaglesong PoW check, timestamp median-time check, epoch check, etc.) instead of an O(1) map lookup rejection. This can be repeated indefinitely across multiple connections, exhausting CPU on the victim node.

**Secondary — Shared best-header state corruption:** If a `BLOCK_INVALID` header happens to pass all sub-checks (e.g., it was marked invalid because its parent was invalid at the time, but the parent's entry was later evicted from `block_status_map`), `insert_valid_header` updates `shared_best_header` to point to an invalid chain tip. This can corrupt the node's fork-choice view and cause it to request blocks on an invalid chain.

**Impact: 4/5** — Realistic CPU-exhaustion DoS against any syncing node; secondary state-corruption path requires a specific race condition.

---

### Likelihood Explanation

Any unprivileged peer can connect and send `SendHeaders` messages. The attacker only needs to know (or craft) a header hash that is already in the victim's `block_status_map` as `BLOCK_INVALID`. This is trivially achievable: the attacker first sends an invalid header (e.g., wrong version, bad PoW), waits for it to be rejected and stored as `BLOCK_INVALID`, then repeatedly re-sends it. No special privilege, key, or majority hashpower is required.

**Likelihood: 4/5**

---

### Recommendation

Add an explicit early-exit for `BLOCK_INVALID` at the top of `HeaderAcceptor::accept()`, resolving the existing `FIXME`:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());

// Early-exit for already-rejected headers
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
    return result;
}

if status.contains(BlockStatus::HEADER_VALID) {
    // ... existing logic
    return result;
}
```

This mirrors the pattern already used in `contextual_check` in the relay path: [7](#0-6) 

---

### Proof of Concept

1. Connect to a CKB node as an unprivileged peer.
2. Send a `SendHeaders` P2P message containing a header with `version != 0` (or any other permanently-invalid field). The node processes it through `HeaderAcceptor::accept()`, fails `version_check`, and stores the hash as `BLOCK_INVALID` in `block_status_map`. [8](#0-7) 

3. Immediately re-send the same header in a new `SendHeaders` message.
4. Observe that the node does **not** short-circuit at the `BLOCK_INVALID` check (the `FIXME` path). Instead it re-runs `prev_block_check`, `non_contextual_check` (full `HeaderVerifier::verify()` including Eaglesong PoW), and `version_check` before finally returning `Invalid`.
5. Repeat step 3 in a tight loop (up to 2000 headers per message). Each iteration forces a full cryptographic re-validation of already-rejected headers, consuming CPU proportional to the number of headers sent. [9](#0-8)

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

**File:** sync/src/synchronizer/headers_process.rs (L106-109)
```rust
        if headers.len() > MAX_HEADERS_LEN {
            warn!("HeadersProcess is oversized");
            return StatusCode::HeadersIsInvalid.with_context("oversize");
        }
```

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

**File:** shared/src/shared.rs (L425-444)
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
