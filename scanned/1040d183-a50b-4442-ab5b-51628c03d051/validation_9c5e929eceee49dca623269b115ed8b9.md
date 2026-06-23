### Title
Stale `is_pending_verify` Flag After `BLOCK_INVALID` Status Set Causes Child Blocks of Invalid Parents to Be Incorrectly Processed as Valid Descendants — (`chain/src/verify.rs` + `chain/src/orphan_broker.rs`)

---

### Summary

In `ConsumeUnverifiedBlockProcessor::consume_unverified_blocks`, the shared `is_pending_verify` flag for a block is cleared **after** the block's status is set to `BLOCK_INVALID`. In the concurrent `OrphanBroker::process_lonely_block`, the `is_pending_verify` flag is checked **before** the `BLOCK_INVALID` status. During the race window between these two operations, a child block of an invalid parent is incorrectly routed into the valid-descendant processing path instead of the invalid-block path, causing unnecessary verification work, temporary incorrect `unverified_tip` advancement, and potential cascading of orphan descendants of an invalid block.

---

### Finding Description

**Thread A — `verify_blocks` thread** (`chain/src/verify.rs`, `consume_unverified_blocks`):

```
verify_block(B) → Err
  → insert_block_status(B, BLOCK_INVALID)   // line 177  ← state written
  ...
  → is_pending_verify.remove(B)             // line 193  ← guard cleared LATE
  → callback(verify_result)                 // line 196
```

**Thread B — `ChainService` thread** (`chain/src/orphan_broker.rs`, `process_lonely_block`):

```rust
let parent_is_pending_verify = self.is_pending_verify.contains(&parent_hash); // line 111
let parent_status = self.shared.get_block_status(&parent_hash);               // line 112
if parent_is_pending_verify || parent_status.contains(BlockStatus::BLOCK_STORED) {
    self.process_descendant(lonely_block);   // line 118 — WRONG branch taken
} else if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
    self.process_invalid_block(lonely_block); // line 120 — never reached
}
```

During the race window (between line 177 and line 193 in Thread A), Thread B can observe `parent_is_pending_verify = true` AND `parent_status = BLOCK_INVALID` simultaneously. Because `is_pending_verify` is checked **first** and short-circuits the `||`, the `BLOCK_INVALID` branch is never evaluated, and `process_descendant` is called for a child of an invalid block.

Note the inconsistency: `search_orphan_leader` (same file, line 42) correctly checks `BLOCK_INVALID` **first** before consulting `is_pending_verify`, but `process_lonely_block` does the opposite.

---

### Impact Explanation

When `process_descendant(C)` is called for a child `C` of an invalid parent `B`:

1. `C` is inserted into `is_pending_verify` (orphan_broker.rs line 200–201).
2. `C` is sent to the preload channel and eventually to the verify thread.
3. `unverified_tip` is advanced to `C`'s block number (orphan_broker.rs line 181–185), causing the sync state machine to believe more work has been accepted than actually has.
4. If `C` has orphan descendants in the orphan pool, `search_orphan_leaders` cascades them all into the same incorrect path.
5. When `C` is eventually verified, it fails (parent is invalid), `set_unverified_tip` rolls back, and `C` is marked `BLOCK_INVALID`.

Concrete effects:
- **Unnecessary CPU/VM verification work** for blocks that should have been immediately rejected.
- **Temporary incorrect `unverified_tip`** causes `find_blocks_to_fetch` to skip fetching legitimate blocks during the window.
- **Cascading**: an attacker who pre-populates the orphan pool with a chain of descendants of an invalid block can amplify the wasted work.
- The peer callback fires with an error for `C` (not `B`), potentially misattributing the punishment.

---

### Likelihood Explanation

The race window is the time between `insert_block_status(BLOCK_INVALID)` (line 177) and `is_pending_verify.remove` (line 193) in the verify thread. An unprivileged sync peer can increase the probability of hitting this window by:

1. Sending an invalid block `B` (e.g., one that passes non-contextual checks but fails contextual verification).
2. Immediately sending one or more child blocks `C1, C2, …` of `B` in rapid succession.

The chain service thread processes incoming blocks from a bounded channel and runs concurrently with the verify thread, so the race is reachable without any privileged access. The window is small but non-zero and grows with verification time (e.g., script execution for a complex block).

---

### Recommendation

**Option 1 (preferred — fix the check order in `process_lonely_block`):** Mirror the logic of `search_orphan_leader` and check `BLOCK_INVALID` before `is_pending_verify`:

```rust
// chain/src/orphan_broker.rs — process_lonely_block
let parent_is_pending_verify = self.is_pending_verify.contains(&parent_hash);
let parent_status = self.shared.get_block_status(&parent_hash);

if parent_status.eq(&BlockStatus::BLOCK_INVALID) {          // check FIRST
    self.process_invalid_block(lonely_block);
} else if parent_is_pending_verify || parent_status.contains(BlockStatus::BLOCK_STORED) {
    self.process_descendant(lonely_block);
} else {
    self.orphan_blocks_broker.insert(lonely_block);
}
```

**Option 2 — clear `is_pending_verify` before setting `BLOCK_INVALID`** in `consume_unverified_blocks` (chain/src/verify.rs line 177 vs 193): move `self.is_pending_verify.remove(&block_hash)` to before `insert_block_status(BLOCK_INVALID)`.

---

### Proof of Concept

**Setup**: Node is syncing. Attacker controls a peer.

1. Attacker sends block `B` at height `N` that passes `non_contextual_verify` but will fail contextual verification (e.g., invalid script, bad total difficulty).
2. `asynchronous_process_block(B)` runs: `insert_block(B)` succeeds (block is stored), `process_lonely_block(B)` routes it to `process_descendant(B)`, inserting `B` into `is_pending_verify` and sending it to the verify thread.
3. Attacker immediately sends child block `C` at height `N+1` (parent = `B`).
4. **Race**: The verify thread begins verifying `B` and calls `insert_block_status(B, BLOCK_INVALID)` (line 177). Before it reaches `is_pending_verify.remove(B)` (line 193), the chain service thread processes `C`.
5. `process_lonely_block(C)`: `is_pending_verify.contains(B)` = **true** → takes `process_descendant(C)` branch → `C` is inserted into `is_pending_verify`, sent to preload channel, `unverified_tip` advanced to `N+1`.
6. Verify thread finishes: `is_pending_verify.remove(B)`.
7. `C` reaches the verify thread, fails (parent `B` is invalid), `set_unverified_tip` rolls back, `C` marked `BLOCK_INVALID`.

Net result: unnecessary verification of `C`, temporary `unverified_tip` at `N+1` instead of the real tip, and any orphan descendants of `C` in the pool are also cascaded. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** chain/src/verify.rs (L139-197)
```rust
        let verify_result = self.verify_block(&block, &parent_header, switch);
        match &verify_result {
            Ok(_) => {
                let log_now = std::time::Instant::now();
                self.shared.remove_block_status(&block_hash);
                let log_elapsed_remove_block_status = log_now.elapsed();
                self.shared.remove_header_view(&block_hash);
                debug!(
                    "block {} remove_block_status cost: {:?}, and header_view cost: {:?}",
                    block_hash,
                    log_elapsed_remove_block_status,
                    log_now.elapsed()
                );
            }
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

                error!(
                    "set_unverified tip to {}-{}, because verify {} failed: {}",
                    tip.number(),
                    tip.hash(),
                    block_hash,
                    err
                );
            }
        }

        self.is_pending_verify.remove(&block_hash);

        if let Some(callback) = verify_callback {
            callback(verify_result);
        }
```

**File:** chain/src/orphan_broker.rs (L39-59)
```rust
    fn search_orphan_leader(&self, leader_hash: ParentHash) {
        let leader_status = self.shared.get_block_status(&leader_hash);

        if leader_status.eq(&BlockStatus::BLOCK_INVALID) {
            let descendants: Vec<LonelyBlockHash> = self
                .orphan_blocks_broker
                .remove_blocks_by_parent(&leader_hash);
            for descendant in descendants {
                self.process_invalid_block(descendant);
            }
            return;
        }

        let leader_is_pending_verify = self.is_pending_verify.contains(&leader_hash);
        if !leader_is_pending_verify && !leader_status.contains(BlockStatus::BLOCK_STORED) {
            trace!(
                "orphan leader: {} not stored {:?} and not in is_pending_verify: {}",
                leader_hash, leader_status, leader_is_pending_verify
            );
            return;
        }
```

**File:** chain/src/orphan_broker.rs (L107-125)
```rust
    pub(crate) fn process_lonely_block(&self, lonely_block: LonelyBlockHash) {
        let block_hash = lonely_block.block_number_and_hash.hash();
        let block_number = lonely_block.block_number_and_hash.number();
        let parent_hash = lonely_block.parent_hash();
        let parent_is_pending_verify = self.is_pending_verify.contains(&parent_hash);
        let parent_status = self.shared.get_block_status(&parent_hash);
        if parent_is_pending_verify || parent_status.contains(BlockStatus::BLOCK_STORED) {
            debug!(
                "parent {} has stored: {:?} or is_pending_verify: {}, processing descendant directly {}-{}",
                parent_hash, parent_status, parent_is_pending_verify, block_number, block_hash,
            );
            self.process_descendant(lonely_block);
        } else if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
            self.process_invalid_block(lonely_block);
        } else {
            self.orphan_blocks_broker.insert(lonely_block);
        }

        self.search_orphan_leaders();
```

**File:** chain/src/orphan_broker.rs (L199-204)
```rust
    fn process_descendant(&self, lonely_block: LonelyBlockHash) {
        self.is_pending_verify
            .insert(lonely_block.block_number_and_hash.hash());

        self.send_unverified_block(lonely_block)
    }
```
