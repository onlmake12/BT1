### Title
`HeaderAcceptor::accept()` Does Not Check `BLOCK_INVALID` Status Before Re-Processing Headers — (`File: sync/src/synchronizer/headers_process.rs`)

---

### Summary

`HeaderAcceptor::accept()` checks only the positive condition (`HEADER_VALID`) to short-circuit processing of already-seen headers, but never checks the negative condition (`BLOCK_INVALID`). A header previously rejected and marked `BLOCK_INVALID` is silently re-accepted, re-inserted into the header map, and used to update the node's sync state. This is the direct CKB analog of the ERC20Gauges bug: checking "not in deprecated set" but not "is in active set."

---

### Finding Description

In `sync/src/synchronizer/headers_process.rs`, `HeaderAcceptor::accept()` begins with a status check:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // already processed as valid → return early
    ...
    return result;
}
``` [1](#0-0) 

The function returns early only when the header is already `HEADER_VALID`. It does **not** return early when the header is `BLOCK_INVALID`. The developers themselves flag this with a `// FIXME` comment. After the early-return check, the function runs `prev_block_check`, `non_contextual_check`, and `version_check`. If all pass, it calls `insert_valid_header`:

```rust
sync_shared.insert_valid_header(self.peer, self.header);
``` [2](#0-1) 

`insert_valid_header` inserts the header into the persistent `header_map`, updates the peer's best-known header, and potentially updates the global shared best header:

```rust
self.shared.header_map().insert(header_view.clone());
self.state.peers().may_set_best_known_header(peer, header_view.as_header_index());
...
self.state.may_set_shared_best_header(header_view);
``` [3](#0-2) 

`get_block_status` resolves status by checking `block_status_map` first, then `header_map`, then the DB:

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
``` [4](#0-3) 

`block_status_map` is an in-memory `DashMap` that is **cleared on node restart**. After a restart, a block previously verified as invalid (stored in the DB as `block_ext.verified = Some(false)`) is no longer in `block_status_map`. Its status comes from the DB path, returning `BLOCK_INVALID`. When a peer sends this header again, `accept()` does not short-circuit, all header-level checks pass (the header itself may be structurally valid even if the block body was invalid), and `insert_valid_header` inserts it into `header_map`. On the next call to `get_block_status`, `block_status_map` has no entry, `header_map` does → status is now `HEADER_VALID`.

The `BlockStatus` flags are defined as:

```rust
const HEADER_VALID  =     1;
const BLOCK_INVALID =     1 << 12;
``` [5](#0-4) 

`BLOCK_INVALID` does not overlap with `HEADER_VALID`, so `status.contains(BlockStatus::HEADER_VALID)` is `false` for `BLOCK_INVALID` — the early-return guard never fires.

The `prev_block_check` sub-check also only checks the negative condition (parent is `BLOCK_INVALID`) without checking the positive condition (parent is `BLOCK_STORED` or `BLOCK_VALID`):

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
}
``` [6](#0-5) 

This mirrors the ERC20Gauges pattern exactly: checking "not in bad set" but not "is in good set."

---

### Impact Explanation

Once a previously-rejected block's header is re-inserted into `header_map` with effective `HEADER_VALID` status, `asynchronous_process_remote_block` will accept the full block from the peer:

```rust
} else if status.contains(BlockStatus::HEADER_VALID) {
    self.shared.accept_remote_block(&self.chain, remote_block);
}
``` [7](#0-6) 

This causes:
1. **Re-verification of a known-invalid block** — full contextual verification including script execution is re-run, consuming significant CPU.
2. **Sync state corruption** — `may_set_best_known_header` and `may_set_shared_best_header` update the node's view of the best chain to point to an invalid block hash, potentially causing the node to request further blocks building on the invalid chain.
3. **Repeatable DoS** — after re-verification fails and `BLOCK_INVALID` is re-inserted into `block_status_map`, the cycle can repeat on the next node restart or if `block_status_map` is evicted.

---

### Likelihood Explanation

The entry path is fully unprivileged: any P2P peer can send `SendHeaders` messages. The `HeadersProcess` handler calls `HeaderAcceptor::accept()` for each received header with no prior authentication. A single malicious peer that knows a block hash previously rejected by the target node (e.g., from a prior sync attempt or public knowledge of an invalid block) can trigger this repeatedly. The condition is reachable after any node restart, since `block_status_map` is not persisted.

---

### Recommendation

Add an explicit `BLOCK_INVALID` early-return guard at the top of `accept()`, resolving the acknowledged `// FIXME`:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::BLOCK_INVALID) {
    // already known invalid, reject immediately
    result.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) {
    ...
    return result;
}
```

Similarly, `prev_block_check` should be strengthened to require the parent to be in a known-valid state (e.g., `BLOCK_STORED` or `BLOCK_VALID`), not merely "not `BLOCK_INVALID`":

```rust
if !self.active_chain.contains_block_status(
    &self.header.data().raw().parent_hash(),
    BlockStatus::BLOCK_STORED,
) {
    state.invalid(Some(ValidationError::InvalidParent));
    return Err(());
}
```

---

### Proof of Concept

1. Node A verifies block B and rejects it (e.g., invalid transaction). `block_ext.verified = Some(false)` is written to DB. `BLOCK_INVALID` is inserted into `block_status_map`.
2. Node A restarts. `block_status_map` is cleared. `get_block_status(B)` now reads from DB → `BLOCK_INVALID`.
3. Malicious peer P connects and sends a `SendHeaders` message containing header of B.
4. `HeadersProcess` calls `HeaderAcceptor::accept()` for B's header.
5. `status = BLOCK_INVALID`. `status.contains(HEADER_VALID)` is `false` → no early return.
6. `prev_block_check`: B's parent is valid → passes.
7. `non_contextual_check`: B's header passes PoW, timestamp, epoch, number checks → passes.
8. `version_check`: passes.
9. `insert_valid_header` is called → B's header is inserted into `header_map`. `may_set_best_known_header(P, B)` and `may_set_shared_best_header(B)` are called.
10. `get_block_status(B)` → not in `block_status_map`, IS in `header_map` → returns `HEADER_VALID`.
11. Peer P sends full block B. `asynchronous_process_remote_block`: status is `HEADER_VALID` → `accept_remote_block` is called → block is re-verified at full cost.
12. Repeat from step 3 after next restart. [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** sync/src/synchronizer/headers_process.rs (L244-253)
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

**File:** shared/src/shared.rs (L425-453)
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

    pub fn contains_block_status<T: ChainStore>(
        &self,
        block_hash: &Byte32,
        status: BlockStatus,
    ) -> bool {
        self.get_block_status(block_hash).contains(status)
    }
```

**File:** shared/src/block_status.rs (L11-16)
```rust
        const HEADER_VALID            =     1;
        const BLOCK_RECEIVED          =     1 | (Self::HEADER_VALID.bits() << 1);
        const BLOCK_STORED            =     1 | (Self::BLOCK_RECEIVED.bits() << 1);
        const BLOCK_VALID             =     1 | (Self::BLOCK_STORED.bits() << 1);

        const BLOCK_INVALID           =     1 << 12;
```

**File:** sync/src/synchronizer/mod.rs (L477-479)
```rust
        } else if status.contains(BlockStatus::HEADER_VALID) {
            self.shared.accept_remote_block(&self.chain, remote_block);
        } else {
```
