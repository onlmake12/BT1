### Title
Missing `BLOCK_INVALID` Guard in `HeaderAcceptor::accept` Allows Sync-State Corruption via Re-sent Invalid Headers - (File: `sync/src/synchronizer/headers_process.rs`)

---

### Summary

`HeaderAcceptor::accept()` checks whether a header's status contains `BLOCK_INVALID` only via a developer-acknowledged `FIXME` comment that was never implemented. When a block has been fully verified and marked `BLOCK_INVALID` (e.g., due to failed script execution), a peer can re-send its header and the node will re-accept it as `HEADER_VALID`, corrupting the sync state machine and causing the node to waste resources chasing an invalid chain.

---

### Finding Description

In `sync/src/synchronizer/headers_process.rs`, `HeaderAcceptor::accept()` reads the current block status and immediately checks only for `HEADER_VALID`:

```rust
// FIXME If status == BLOCK_INVALID then return early. But which error
// type should we return?
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    // update peer best_known_header and return
    ...
    return result;
}
```

`BLOCK_INVALID` is defined as `1 << 12`, a completely separate bit from the `HEADER_VALID` chain (`1`, `3`, `7`, `15`):

```rust
const HEADER_VALID  =     1;
const BLOCK_INVALID =     1 << 12;
```

Because `BLOCK_INVALID` does not overlap with `HEADER_VALID`, `status.contains(BlockStatus::HEADER_VALID)` returns `false` for an invalid block, and the function falls through to `prev_block_check`, `non_contextual_check`, and `version_check`. A block that was marked `BLOCK_INVALID` due to **contextual** failure (e.g., script execution, transaction-level errors) will have a structurally valid header that passes all three header-only checks. The function then calls `insert_valid_header`, which:

1. Inserts the header into `header_map` (making it appear as a known valid header)
2. Updates the peer's `best_known_header` to point to this invalid chain
3. Potentially updates the global `shared_best_header` if the invalid chain has higher total difficulty

The `FIXME` comment at line 301 is the developers' own acknowledgment that this guard is missing.

---

### Impact Explanation

- **Sync state corruption**: The peer's `best_known_header` and potentially the global `shared_best_header` are updated to point to a chain rooted in a previously-rejected block. The `BlockFetcher` will then schedule download requests for blocks on this invalid chain.
- **Resource exhaustion**: The node sends `GetBlocks` requests for blocks that will be silently dropped when received (because `asynchronous_process_remote_block` checks `HEADER_VALID` exactly, and the block's status in `block_status_map` remains `BLOCK_INVALID`). A malicious peer can sustain this loop indefinitely.
- **IBD disruption**: During Initial Block Download, the node selects sync peers based on `best_known_header`. Corrupting this value can cause the node to waste its IBD slot on a peer advertising an invalid chain, stalling synchronization.

---

### Likelihood Explanation

Any unprivileged P2P peer can send `SendHeaders` messages. The attack requires only that the attacker previously observed (or crafted) a block that was accepted into the chain pipeline, stored, and then failed contextual verification — a realistic scenario since blocks are stored before full script verification in the async pipeline. The `FIXME` comment confirms the developers are aware the guard is absent. No special privileges, keys, or majority hashpower are required.

---

### Recommendation

Add an explicit `BLOCK_INVALID` early-return at the top of `HeaderAcceptor::accept()`, before the `HEADER_VALID` check:

```rust
pub fn accept(&self) -> ValidationResult {
    let mut result = ValidationResult::default();
    let sync_shared = self.active_chain.sync_shared();
    let state = self.active_chain.state();
    let shared = sync_shared.shared();

    let status = self.active_chain.get_block_status(&self.header.hash());

+   // If the block was previously determined to be invalid, reject immediately.
+   if status.contains(BlockStatus::BLOCK_INVALID) {
+       result.invalid(Some(ValidationError::InvalidParent)); // or a dedicated variant
+       return result;
+   }

    if status.contains(BlockStatus::HEADER_VALID) {
        ...
    }
    ...
}
```

---

### Proof of Concept

1. Attacker connects as a P2P peer.
2. Attacker submits a block `B` whose header is structurally valid (correct PoW, valid timestamp, version 0) but whose transactions fail script execution.
3. The node stores `B` (`BLOCK_STORED`), then the `ConsumeUnverifiedBlockProcessor` runs full verification, fails, and sets `B`'s status to `BLOCK_INVALID` in `block_status_map`.
4. Attacker sends a `SendHeaders` message containing `B`'s header.
5. `HeaderAcceptor::accept()` reads status = `BLOCK_INVALID` (= `4096`). `4096 & 1 == 0`, so `status.contains(HEADER_VALID)` is `false` — the early-return is skipped.
6. `prev_block_check`: `B`'s parent is valid → passes.
7. `non_contextual_check`: `B`'s header passes `HeaderVerifier` → passes.
8. `version_check`: version == 0 → passes.
9. `insert_valid_header` is called: `B`'s header is inserted into `header_map`; the peer's `best_known_header` is set to `B`; `shared_best_header` may be updated.
10. `BlockFetcher` now schedules `GetBlocks` for `B` and its descendants, which are silently dropped on receipt, wasting bandwidth and CPU indefinitely.

---

**Root cause file:** `sync/src/synchronizer/headers_process.rs`, `HeaderAcceptor::accept()`, line 301–322. [1](#0-0) 

**Supporting — `BLOCK_INVALID` bit definition:** [2](#0-1) 

**Supporting — `insert_valid_header` updates peer state without checking `BLOCK_INVALID`:** [3](#0-2) 

**Supporting — contextual verification sets `BLOCK_INVALID` after storage:** [4](#0-3)

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

**File:** chain/src/verify.rs (L153-181)
```rust
            Err(err) => {
                error!("verify block {} failed: {}", block_hash, err);

                let tip = self
                    .shared
                    .store()
                    .get_tip_header()
                    .expect("tip_header must exist");
                let tip_ext = self
                    .shared
                    .store()
                    .get_block_ext(&tip.hash())
                    .expect("tip header's ext must exist");

                self.shared.set_unverified_tip(ckb_shared::HeaderIndex::new(
                    tip.number(),
                    tip.hash(),
                    tip_ext.total_difficulty,
                ));

                self.delete_unverified_block(&block);

                if !is_internal_db_error(err) {
                    self.shared
                        .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
                } else {
                    error!("internal db error, remove block status: {}", block_hash);
                    self.shared.remove_block_status(&block_hash);
                }
```
