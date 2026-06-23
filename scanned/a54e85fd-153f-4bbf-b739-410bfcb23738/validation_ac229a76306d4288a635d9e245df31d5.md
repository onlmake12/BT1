### Title
`ValidationState::Valid` Default Equals Success State Allows `BLOCK_INVALID` Headers to Be Re-accepted as Valid — (`sync/src/synchronizer/headers_process.rs`)

---

### Summary

`ValidationState` uses `Valid` as its Rust `#[default]`, the same value returned on successful header acceptance. A known-`BLOCK_INVALID` header (stored in the DB after a node restart) bypasses the missing early-return guard (acknowledged by a `FIXME` comment) in `HeaderAcceptor::accept()`, passes all three structural checks, and causes `insert_valid_header` to be called. This inserts the header into the in-memory `header_map`, which has higher lookup priority than the DB, silently overriding the `BLOCK_INVALID` status to `HEADER_VALID` for the remainder of the session.

---

### Finding Description

**Root cause — default state equals success state** [1](#0-0) 

`ValidationState::Valid` is the `#[default]` variant. `ValidationResult::default()` therefore starts as `Valid` with no validation performed — there is no `Uninitialized` sentinel to distinguish "not yet evaluated" from "explicitly accepted."

**The unguarded FIXME path** [2](#0-1) 

`HeaderAcceptor::accept()` creates `result = ValidationResult::default()` (i.e., `Valid`), then reads the block's status. The comment at line 301–302 explicitly acknowledges the missing guard:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
```

Because no early return exists for `BLOCK_INVALID`, the function falls through to `prev_block_check` (checks only the *parent*), `non_contextual_check` (structural header verification), and `version_check`. A structurally valid header for a previously-rejected block passes all three. `insert_valid_header` is then called and the function returns `ValidationResult { state: Valid }`.

**Status lookup priority — `header_map` shadows the DB** [3](#0-2) 

`get_block_status` resolves in this order:
1. In-memory `block_status_map` (cleared on restart)
2. In-memory `header_map` (cleared on restart)
3. DB (`block_ext.verified`)

After a node restart both in-memory maps are empty. A block stored in the DB with `verified = Some(false)` returns `BLOCK_INVALID`. Once `insert_valid_header` inserts the header into `header_map`, subsequent calls to `get_block_status` return `HEADER_VALID` — the DB-backed `BLOCK_INVALID` is permanently shadowed for the session. [4](#0-3) 

`insert_valid_header` writes only to `header_map` and never touches `block_status_map`, so the `BLOCK_INVALID` entry in the DB is never cleared but is also never consulted again once `header_map` has an entry.

**Downstream consequence — compact block and full block re-processing** [5](#0-4) 

`contextual_check` rejects a compact block only if `status.contains(BlockStatus::BLOCK_INVALID)`. After the header is re-inserted, the status is `HEADER_VALID`, so this guard is bypassed and the compact block is processed. [6](#0-5) 

`asynchronous_process_remote_block` similarly accepts the full block when status is `HEADER_VALID`, calling `accept_remote_block` and re-queuing the block for chain processing.

---

### Impact Explanation

An unprivileged peer can send a `SendHeaders` P2P message containing the header of any block the local node previously rejected and stored as `BLOCK_INVALID` in the DB. After a node restart (which clears the in-memory `block_status_map`), the node re-accepts the header as `HEADER_VALID`, overrides the persisted invalid status for the session, and will re-request and re-process the full block or compact block. This wastes CPU, memory, and bandwidth. More critically, the node's `shared_best_header` and per-peer best-known-header state may be updated to reflect a chain that includes a known-invalid block, corrupting sync decisions until the block fails validation again and the status is re-set.

---

### Likelihood Explanation

Medium. The precondition — a block stored in the DB with `verified = Some(false)` — arises naturally during async block processing when a block fails full validation after being stored. A node restart is a routine operational event. Any peer that observed the original block can replay its header. The `FIXME` comment in the production code explicitly acknowledges the missing guard, confirming the developers are aware the path is reachable but have not yet resolved it.

---

### Recommendation

1. **Add an `Uninitialized` variant** to `ValidationState` and make it the `#[default]`, so that a freshly created `ValidationResult` is never silently treated as a success:

```rust
#[derive(Default, Debug, Copy, Clone, PartialEq, Eq)]
pub enum ValidationState {
    #[default]
    Uninitialized,   // ← new sentinel; never returned to callers
    Valid,
    TemporaryInvalid,
    Invalid,
}
```

2. **Fix the FIXME** in `HeaderAcceptor::accept()` by returning early when `status.contains(BlockStatus::BLOCK_INVALID)`, setting `result.state = ValidationState::Invalid` before returning:

```rust
if status.contains(BlockStatus::BLOCK_INVALID) {
    result.invalid(None);
    return result;
}
```

3. Assert in callers that a returned `ValidationResult` is never `Uninitialized`, making any future omission a compile-time or runtime error rather than a silent pass.

---

### Proof of Concept

1. Run a CKB node and let it process a block that fails full validation (e.g., a block with an invalid script). The block is stored in the DB with `block_ext.verified = Some(false)`.
2. Restart the node. `block_status_map` and `header_map` are now empty.
3. Connect as an unprivileged peer and send a `SendHeaders` message containing the header of the rejected block.
4. `HeadersProcess::execute()` calls `HeaderAcceptor::accept()`. `get_block_status` returns `BLOCK_INVALID` (from DB). The FIXME path is taken: no early return. `prev_block_check`, `non_contextual_check`, and `version_check` all pass. `insert_valid_header` is called.
5. Call `get_block_status` for the block hash. Observe it now returns `HEADER_VALID` instead of `BLOCK_INVALID`.
6. Send a compact block for the same block. `contextual_check` no longer rejects it (the `BLOCK_INVALID` guard is bypassed). The node re-processes the block, wasting resources and temporarily corrupting its sync state.

### Citations

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

**File:** sync/src/synchronizer/headers_process.rs (L361-367)
```rust
#[derive(Default, Debug, Copy, Clone, PartialEq, Eq)]
pub enum ValidationState {
    #[default]
    Valid,
    TemporaryInvalid,
    Invalid,
}
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

**File:** sync/src/relayer/compact_block_process.rs (L240-261)
```rust
    let status = active_chain.get_block_status(&block_hash);
    if status.contains(BlockStatus::BLOCK_STORED) {
        // update last common header and best known
        let parent = shared
            .get_header_index_view(&compact_block_header.data().raw().parent_hash(), true)
            .expect("parent block must exist");

        let header_index = HeaderIndex::new(
            compact_block_header.number(),
            block_hash.clone(),
            parent.total_difficulty() + compact_block_header.difficulty(),
        );
        let state = shared.state().peers();
        state.may_set_best_known_header(peer, header_index);

        return StatusCode::CompactBlockAlreadyStored.with_context(block_hash);
    } else if status.contains(BlockStatus::BLOCK_RECEIVED) {
        // block already in orphan pool
        return Status::ignored();
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```

**File:** sync/src/synchronizer/mod.rs (L468-486)
```rust
    /// Process a new block sync from other peer
    //TODO: process block which we don't request
    pub fn asynchronous_process_remote_block(&self, remote_block: RemoteBlock) {
        let block_hash = remote_block.block.hash();
        let status = self.shared.active_chain().get_block_status(&block_hash);
        // NOTE: Filtering `BLOCK_STORED` but not `BLOCK_RECEIVED`, is for avoiding
        // stopping synchronization even when orphan_pool maintains dirty items by bugs.
        if status.contains(BlockStatus::BLOCK_STORED) {
            error!("Block {} already stored", block_hash);
        } else if status.contains(BlockStatus::HEADER_VALID) {
            self.shared.accept_remote_block(&self.chain, remote_block);
        } else {
            debug!(
                "Synchronizer process_new_block unexpected status {:?} {}",
                status, block_hash,
            );
            // TODO which error should we return?
        }
    }
```
