### Title
Missing `BLOCK_INVALID` Status Check in `HeaderAcceptor::accept()` Causes Inconsistent Block Status Enforcement — (`sync/src/synchronizer/headers_process.rs`)

---

### Summary

`HeaderAcceptor::accept()` in `sync/src/synchronizer/headers_process.rs` does not check for `BlockStatus::BLOCK_INVALID` before re-processing a received header. This is inconsistent with every other status-check site in the sync/relay pipeline, which all guard against `BLOCK_INVALID` first. The gap is explicitly acknowledged by a developer `// FIXME` comment in the code. An unprivileged remote peer can exploit this by re-sending headers that the node has already marked invalid, causing the node to re-run validation, re-insert them as `HEADER_VALID`, and then re-attempt full-block downloads for blocks it already knows are invalid.

---

### Finding Description

`BlockStatus` is a bitflag type defined in `shared/src/block_status.rs`:

```rust
pub struct BlockStatus: u32 {
    const UNKNOWN       =     0;
    const HEADER_VALID  =     1;
    const BLOCK_RECEIVED=     1 | (Self::HEADER_VALID.bits() << 1);
    const BLOCK_STORED  =     1 | (Self::BLOCK_RECEIVED.bits() << 1);
    const BLOCK_VALID   =     1 | (Self::BLOCK_STORED.bits() << 1);
    const BLOCK_INVALID =     1 << 12;   // ← isolated bit, no overlap
}
``` [1](#0-0) 

Because `BLOCK_INVALID = 4096` shares no bits with the `HEADER_VALID` chain, `status.contains(HEADER_VALID)` is `false` when `status == BLOCK_INVALID`. This means the early-return guard in `accept()` does **not** fire for invalid headers.

`HeaderAcceptor::accept()` in `headers_process.rs` reads:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // ... update best_known_header and return
    return result;
}
// Falls through to full re-validation for BLOCK_INVALID headers
if self.prev_block_check(&mut result).is_err() { ... }
if let Some(is_invalid) = self.non_contextual_check(&mut result).err() { ... }
if self.version_check(&mut result).is_err() { ... }
sync_shared.insert_valid_header(self.peer, self.header);  // ← reached if checks pass
``` [2](#0-1) 

Every other status-check site in the pipeline guards against `BLOCK_INVALID` explicitly and returns early:

- `contextual_check()` in `compact_block_process.rs`:
  ```rust
  } else if status.contains(BlockStatus::BLOCK_INVALID) {
      return StatusCode::BlockIsInvalid.with_context(block_hash);
  }
  ``` [3](#0-2) 

- `process_lonely_block()` in `orphan_broker.rs`:
  ```rust
  } else if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
      self.process_invalid_block(lonely_block);
  }
  ``` [4](#0-3) 

- `verify_block()` in `verify.rs`:
  ```rust
  if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
      return Err(...);
  }
  ``` [5](#0-4) 

`accept()` is the only site that omits this guard, and the `// FIXME` comment confirms the developers are aware of the gap but left it unresolved.

The `BLOCK_INVALID` status is written into `block_status_map` (an in-memory `DashMap`) by multiple paths:

- `compact_block_process.rs` marks a header `BLOCK_INVALID` when compact-block header verification fails (e.g., invalid block number, invalid parent, failed median-time check).
- `chain_service.rs` marks a block `BLOCK_INVALID` when non-contextual verification fails.
- `headers_process.rs` itself marks a header `BLOCK_INVALID` when `prev_block_check` or `version_check` fails. [6](#0-5) 

When `get_block_status()` is called and the hash is not in `block_status_map`, it falls back to the database (`get_block_ext`). A hash that is in `block_status_map` as `BLOCK_INVALID` will return that status directly. [7](#0-6) 

---

### Impact Explanation

**Scenario — re-elevation of a contextually-invalid header to `HEADER_VALID`:**

1. A peer sends a compact block whose header H passes non-contextual checks but fails a contextual check (e.g., median-time-past rule). `compact_block_process.rs` marks H as `BLOCK_INVALID` in `block_status_map`.
2. The same or a different peer sends a `SendHeaders` message containing H.
3. `HeadersProcess::execute()` calls `HeaderAcceptor::accept()` for H.
4. `status = BLOCK_INVALID` (4096). `status.contains(HEADER_VALID)` is false → no early return.
5. `prev_block_check` passes (parent is valid). `non_contextual_check` passes (header is non-contextually valid). `version_check` passes.
6. `insert_valid_header()` is called: H is inserted into `header_map`, the peer's `best_known_header` is updated to H.
7. The block fetcher now considers H a valid download target and issues `GetBlocks` for H.
8. The full block arrives, fails contextual verification, is marked `BLOCK_INVALID` again — but bandwidth and CPU were wasted, and the cycle can repeat. [8](#0-7) 

**Secondary impact — CPU waste from repeated re-validation:**

A malicious peer can send batches of headers already known to be `BLOCK_INVALID`. Each call to `accept()` runs `prev_block_check`, `non_contextual_check`, and `version_check` before re-marking them invalid. This is wasted work that other status-check sites avoid by returning early.

---

### Likelihood Explanation

Any unprivileged P2P peer can send `SendHeaders` messages at will. The attacker does not need to know which specific hashes are in `block_status_map`; they can simply re-send any headers they previously advertised. The compact-block relay path is a natural source of headers that end up `BLOCK_INVALID` on the receiving node. The attack requires no special privileges, no key material, and no majority hashpower.

---

### Recommendation

Add an explicit `BLOCK_INVALID` guard at the top of `HeaderAcceptor::accept()`, consistent with every other status-check site in the codebase:

```rust
pub fn accept(&self) -> ValidationResult {
    let mut result = ValidationResult::default();
    let status = self.active_chain.get_block_status(&self.header.hash());

    // Guard added — consistent with compact_block_process, orphan_broker, verify
    if status.contains(BlockStatus::BLOCK_INVALID) {
        result.invalid(None);
        return result;
    }

    if status.contains(BlockStatus::HEADER_VALID) {
        // ... existing early-return path
        return result;
    }
    // ... rest of validation
}
```

This resolves the `// FIXME` comment and makes the status-check pattern uniform across the entire sync pipeline.

---

### Proof of Concept

1. Connect to a CKB node as a P2P peer using the Sync protocol.
2. Send a `SendHeaders` message containing a header H whose parent is valid but whose compact-block reconstruction would fail a contextual check (e.g., construct a header with a valid PoW but a `timestamp` that violates the median-time-past rule).
3. Observe that the node's `block_status_map` marks H as `BLOCK_INVALID` (visible via debug logging or by observing that a subsequent compact-block send for H returns `BlockIsInvalid`).
4. Send a second `SendHeaders` message containing the same header H.
5. Observe that the node calls `insert_valid_header` for H (visible via the `"inserted valid header"` log line at multiples of 10 000, or by observing a subsequent `GetBlocks` request for H's block hash), demonstrating that the `BLOCK_INVALID` status was not respected and the header was re-elevated to `HEADER_VALID`. [9](#0-8) [10](#0-9)

### Citations

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

**File:** sync/src/relayer/compact_block_process.rs (L256-261)
```rust
    } else if status.contains(BlockStatus::BLOCK_RECEIVED) {
        // block already in orphan pool
        return Status::ignored();
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```

**File:** chain/src/orphan_broker.rs (L119-120)
```rust
        } else if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
            self.process_invalid_block(lonely_block);
```

**File:** chain/src/verify.rs (L244-252)
```rust
            let parent_status = self.shared.get_block_status(&parent_hash);
            if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
                return Err(InternalErrorKind::Other
                    .other(format!(
                        "block: {}'s parent: {} previously verified failed",
                        block_hash, parent_hash
                    ))
                    .into());
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

**File:** shared/src/shared.rs (L455-457)
```rust
    pub fn insert_block_status(&self, block_hash: Byte32, status: BlockStatus) {
        self.block_status_map.insert(block_hash, status);
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
