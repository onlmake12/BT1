### Title
`BLOCK_INVALID` Status Not Checked in `HeaderAcceptor::accept()`, Allowing Re-Processing of Previously-Invalidated Headers - (File: `sync/src/synchronizer/headers_process.rs`)

---

### Summary

`HeaderAcceptor::accept()` sets `BLOCK_INVALID` in the shared `block_status_map` when a header fails validation, but a subsequent call to `accept()` for the same header hash does **not** check for `BLOCK_INVALID` before re-running all validation checks. An explicit `FIXME` comment in the code acknowledges this gap. An unprivileged sync peer can exploit this to force a node to re-process a previously-invalidated header, potentially corrupting the node's sync state by inserting the header as valid via `insert_valid_header`.

---

### Finding Description

In `HeaderAcceptor::accept()`, the code reads the current block status and short-circuits only for `HEADER_VALID`:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // ... update peer best known and return
    return result;
}
``` [1](#0-0) 

`BLOCK_INVALID` (bit `1 << 12`) does not overlap with `HEADER_VALID` (bit `1`), so `status.contains(BlockStatus::HEADER_VALID)` is `false` when the status is `BLOCK_INVALID`. The function then falls through to `prev_block_check`, `non_contextual_check`, and `version_check` as if the header had never been seen before. [2](#0-1) 

If all three checks pass on the second attempt (e.g., the parent's status changed between the first and second call), the function reaches `sync_shared.insert_valid_header(self.peer, self.header)`, which inserts the header into the `header_map`, updates `shared_best_header`, and updates the peer's best-known header — overwriting the `BLOCK_INVALID` state with `HEADER_VALID`. [3](#0-2) 

`insert_valid_header` inserts into `header_map` and calls `may_set_shared_best_header`, which can update the node's view of the best chain: [4](#0-3) 

The `BLOCK_INVALID` status is set in three places within `accept()` itself: [5](#0-4) 

The analog to the external report is exact: `cancelOrder` updates `cancelled[hash]` but `validateOrderParam` never reads it. Here, `accept()` writes `BLOCK_INVALID` to `block_status_map` but the next call to `accept()` for the same hash never reads `BLOCK_INVALID` before proceeding.

---

### Impact Explanation

A previously-invalidated header (status `BLOCK_INVALID`) can be re-submitted by any sync peer via a `SendHeaders` P2P message. If the re-validation passes (the most realistic scenario: the header was marked invalid because its parent was `BLOCK_INVALID`, but the parent's status was later cleared or changed), `insert_valid_header` is called, which:

1. Inserts the header into the in-memory `header_map` with a computed total difficulty.
2. Updates `shared_best_header` if the header's total difficulty exceeds the current best.
3. Updates the peer's `best_known_header`.

This corrupts the node's sync state: the node may believe a previously-rejected chain is the best chain, triggering block download requests for blocks that will ultimately fail full contextual verification. This causes wasted bandwidth, CPU, and can stall or misdirect the sync process. The `BLOCK_INVALID` status in `block_status_map` is in-memory only (not persisted), so the window is open for the entire node session.

---

### Likelihood Explanation

Any unprivileged peer connected via the sync protocol can send `SendHeaders` messages. The `HeadersProcess::execute()` function processes each header in the message through `HeaderAcceptor::accept()`: [6](#0-5) 

The most realistic trigger: during IBD or normal sync, a peer sends a batch of headers where the first header's parent is temporarily unknown. The node marks the header `BLOCK_INVALID` via `prev_block_check`. Later, the parent is resolved and its status is no longer `BLOCK_INVALID`. The same peer (or another) re-sends the header. The `BLOCK_INVALID` check is skipped, `prev_block_check` now passes, and the header is inserted as valid. No special privileges are required — only a standard P2P connection.

---

### Recommendation

Add an early-return guard for `BLOCK_INVALID` at the top of `accept()`, immediately after reading the status:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(None); // or a dedicated InvalidStatus error variant
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) {
    // ... existing logic
    return result;
}
```

This mirrors the pattern already used in `contextual_check` in `compact_block_process.rs`: [7](#0-6) 

---

### Proof of Concept

1. Connect an attacker-controlled peer to a CKB node via the sync protocol.
2. Send a `SendHeaders` message containing header `H` whose parent `P` is currently `BLOCK_INVALID` in the node's `block_status_map`.
3. `HeaderAcceptor::accept()` is called for `H`. `prev_block_check` detects `P` is `BLOCK_INVALID`, sets `H`'s status to `BLOCK_INVALID`, and returns `ValidationState::Invalid`.
4. Wait for or arrange for `P`'s status to be removed from `block_status_map` (e.g., node restart clears the map, or `remove_block_status` is called during reorg cleanup as seen in `chain/src/verify.rs` line 143).
5. Re-send the same `SendHeaders` message containing `H`.
6. `accept()` is called again. `status` is now `BLOCK_INVALID` (if still in map) or `UNKNOWN` (if cleared). In either case, `status.contains(BlockStatus::HEADER_VALID)` is `false`, so the early-return is skipped.
7. `prev_block_check` now passes (parent `P` is no longer `BLOCK_INVALID`). `non_contextual_check` and `version_check` pass for a well-formed header.
8. `sync_shared.insert_valid_header(peer, H)` is called — the previously-invalidated header `H` is now inserted as valid, updating `shared_best_header` and the peer's best-known header. [8](#0-7) [9](#0-8)

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

**File:** sync/src/types/mod.rs (L1094-1141)
```rust
    pub fn insert_valid_header(&self, peer: PeerIndex, header: &core::HeaderView) {
        let tip_number = self.active_chain().tip_number();
        let store_first = tip_number >= header.number();
        // We don't use header#parent_hash clone here because it will hold the arc counter of the SendHeaders message
        // which will cause the 2000 headers to be held in memory for a long time
        let parent_hash = Byte32::from_slice(header.data().raw().parent_hash().as_slice())
            .expect("checked slice length");
        let parent_header_index = self
            .get_header_index_view(&parent_hash, store_first)
            .expect("parent should be verified");
        let mut header_view = HeaderIndexView::new(
            header.hash(),
            header.number(),
            header.epoch(),
            header.timestamp(),
            parent_hash,
            parent_header_index.total_difficulty() + header.difficulty(),
        );

        let snapshot = Arc::clone(&self.shared.snapshot());
        header_view.build_skip(
            tip_number,
            |hash, store_first| self.get_header_index_view(hash, store_first),
            |number, current| {
                // shortcut to return an ancestor block
                if current.number <= snapshot.tip_number() && snapshot.is_main_chain(&current.hash)
                {
                    snapshot
                        .get_block_hash(number)
                        .and_then(|hash| self.get_header_index_view(&hash, true))
                } else {
                    None
                }
            },
        );
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

**File:** chain/src/verify.rs (L143-143)
```rust
                self.shared.remove_block_status(&block_hash);
```
