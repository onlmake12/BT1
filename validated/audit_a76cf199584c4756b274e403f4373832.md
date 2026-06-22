### Title
`HeaderAcceptor::accept()` Silently Re-validates `BLOCK_INVALID` Headers, Corrupting Peer Sync State - (File: `sync/src/synchronizer/headers_process.rs`)

---

### Summary

`HeaderAcceptor::accept()` in the sync subsystem contains an acknowledged `FIXME` where a header whose block was previously marked `BLOCK_INVALID` is not rejected early. Because `BlockStatus::BLOCK_INVALID` (`1 << 12`) does not contain the `BlockStatus::HEADER_VALID` (`1`) bit, the early-return guard is bypassed. The function then re-runs only the lightweight non-contextual checks, and if those pass (which they will for blocks that were invalidated for contextual reasons), it calls `insert_valid_header`, inserting the invalid block's header into the header map and updating the peer's `best_known_header` to point at a known-invalid block.

---

### Finding Description

In `sync/src/synchronizer/headers_process.rs`, `HeaderAcceptor::accept()` begins with:

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

The `BlockStatus` flags are defined as independent bit-fields:

```
HEADER_VALID  = 1
BLOCK_INVALID = 1 << 12  (= 4096)
``` [2](#0-1) 

`BLOCK_INVALID` does **not** contain the `HEADER_VALID` bit. Therefore `status.contains(BlockStatus::HEADER_VALID)` evaluates to `false` when the block is `BLOCK_INVALID`, and the early-return is skipped. The code falls through to three lightweight checks:

1. `prev_block_check` — only checks whether the *parent* is `BLOCK_INVALID`
2. `non_contextual_check` — runs `HeaderVerifier` (PoW nonce, timestamp, epoch continuity, version)
3. `version_check` — checks `header.version() == 0` [3](#0-2) 

A block can be marked `BLOCK_INVALID` for contextual reasons that are entirely invisible to these three checks: invalid transactions, invalid cellbase reward, invalid DAO header, invalid two-phase commit, etc. For such a block, all three checks pass, and the function reaches:

```rust
sync_shared.insert_valid_header(self.peer, self.header);
``` [4](#0-3) 

`insert_valid_header` inserts the header into the `header_map`, calls `may_set_best_known_header` to update the peer's best-known chain tip, and potentially calls `may_set_shared_best_header` to update the global shared best header — all for a block the node already knows is invalid. [5](#0-4) 

By contrast, the `CompactBlockProcess` path correctly checks `BLOCK_INVALID` explicitly and returns an error:

```rust
} else if status.contains(BlockStatus::BLOCK_INVALID) {
    return StatusCode::BlockIsInvalid.with_context(block_hash);
}
``` [6](#0-5) 

The `SendHeaders` path has no equivalent guard.

---

### Impact Explanation

An attacker peer that previously caused a block to be marked `BLOCK_INVALID` (e.g., by relaying a block with invalid transactions or an invalid reward) can subsequently re-send that block's header via the `SendHeaders` P2P message. The node will:

1. Insert the invalid block's header into its `header_map` as if it were valid.
2. Update the peer's `best_known_header` to the invalid block.
3. Potentially update `shared_best_header` if the invalid block has higher total difficulty than the current best.

Consequence (a): The `BlockFetcher` uses `best_known_header` to decide which blocks to download. With it pointing at an invalid block, the node will issue `GetBlocks` requests for blocks on a chain it already knows is invalid, wasting bandwidth and CPU. [7](#0-6) 

Consequence (b): If `shared_best_header` is corrupted, the node's IBD state machine and sync scheduling are affected, potentially stalling legitimate sync or causing repeated futile download attempts.

Consequence (c): The `header_map` is polluted with entries for invalid blocks, which can affect ancestor-traversal and skip-pointer logic used throughout the sync subsystem.

---

### Likelihood Explanation

The entry path is the standard `SendHeaders` P2P message, processed by `HeadersProcess::execute()` → `HeaderAcceptor::accept()`. Any connected peer can send this message without any privilege. The attacker only needs to:

1. Have previously caused a block to be marked `BLOCK_INVALID` (e.g., by relaying a contextually invalid block — one with a valid header but invalid body).
2. Re-send the same block's header via `SendHeaders`.

Step 1 is achievable by any peer that can construct a block with a valid PoW header but invalid body (e.g., wrong cellbase reward). The FIXME comment in the source code confirms the developers are aware of the gap.

---

### Recommendation

Add an explicit `BLOCK_INVALID` guard at the top of `HeaderAcceptor::accept()`, before the `HEADER_VALID` check, analogous to what `CompactBlockProcess` already does:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::BLOCK_INVALID) {
    state.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
    return result;
}
if status.contains(BlockStatus::HEADER_VALID) {
    ...
}
```

This mirrors the fix described in the reference report: complement the `if` with an `else` branch (or a preceding guard) that handles the failure case explicitly rather than silently falling through.

---

### Proof of Concept

1. Connect a malicious peer to a CKB node.
2. Relay a block whose header is valid (correct PoW, timestamp, epoch) but whose body is contextually invalid (e.g., cellbase output exceeds the allowed reward). The node processes it, fails contextual verification, and sets `block_status_map[block_hash] = BLOCK_INVALID`.
3. Send a `SendHeaders` P2P message containing that same block's header.
4. `HeadersProcess::execute()` calls `HeaderAcceptor::accept()`.
5. `get_block_status` returns `BLOCK_INVALID` (= `4096`). `status.contains(HEADER_VALID)` = `(4096 & 1) != 0` = `false`. The early-return is skipped.
6. `prev_block_check` passes (the parent is valid). `non_contextual_check` passes (the header's PoW/timestamp/epoch are valid). `version_check` passes.
7. `insert_valid_header` is called: the invalid block's header is inserted into `header_map`, and `best_known_header` for the peer is updated to the invalid block.
8. Observe via `get_peers` RPC that `best_known_header_hash` now points to the known-invalid block. [8](#0-7) [9](#0-8)

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

**File:** sync/src/types/mod.rs (L873-883)
```rust
    pub fn may_set_best_known_header(&self, peer: PeerIndex, header_index: HeaderIndex) {
        if let Some(mut peer_state) = self.state.get_mut(&peer) {
            if let Some(ref known) = peer_state.best_known_header {
                if header_index.is_better_chain(known) {
                    peer_state.best_known_header = Some(header_index);
                }
            } else {
                peer_state.best_known_header = Some(header_index);
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

**File:** sync/src/relayer/compact_block_process.rs (L259-261)
```rust
    } else if status.contains(BlockStatus::BLOCK_INVALID) {
        return StatusCode::BlockIsInvalid.with_context(block_hash);
    }
```

**File:** sync/src/synchronizer/block_fetcher.rs (L159-169)
```rust
        let best_known = match self.peer_best_known_header() {
            Some(t) => t,
            None => {
                debug!(
                    "Peer {} doesn't have best known header; ignore it",
                    self.peer
                );
                return None;
            }
        };
        if !best_known.is_better_than(self.active_chain.total_difficulty()) {
```
