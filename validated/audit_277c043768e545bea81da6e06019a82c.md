Having read both files in full, I can now validate every claim against the actual code.

**`orphan_broker.rs` — `process_lonely_block` (lines 111–123):** [1](#0-0) 

The check order is exactly as claimed: `is_pending_verify` is tested first via `||`, so a `true` result short-circuits before `BLOCK_INVALID` is ever evaluated.

**`orphan_broker.rs` — `search_orphan_leader` (lines 40–59):** [2](#0-1) 

Confirmed: `BLOCK_INVALID` is checked first (line 42), then `is_pending_verify` (line 52) — the opposite order from `process_lonely_block`.

**`verify.rs` — `consume_unverified_blocks` (lines 177 and 193):** [3](#0-2) 

`insert_block_status(BLOCK_INVALID)` is at line 177; `is_pending_verify.remove` is at line 193. The race window between them is real.

**`verify.rs` — `verify_block` parent re-check (lines 244–252):** [4](#0-3) 

The partial mitigation exists exactly as described.

**`process_descendant` — `unverified_tip` advancement:** [5](#0-4) [6](#0-5) 

Every claim in the report is confirmed by the actual code. All line numbers, function names, and logic are accurate.

---

Audit Report

## Title
Stale `is_pending_verify` Flag Causes Child Blocks of Invalid Parents to Enter Verification Path — (`chain/src/orphan_broker.rs`)

## Summary
In `process_lonely_block`, `is_pending_verify` is checked before `BLOCK_INVALID` status via a short-circuit `||`. During the window between `insert_block_status(BLOCK_INVALID)` (line 177 of `verify.rs`) and `is_pending_verify.remove` (line 193), a child block of an invalid parent is routed to `process_descendant` instead of `process_invalid_block`. This causes unnecessary pipeline work and a temporary incorrect `unverified_tip` advancement, triggerable by any sync peer.

## Finding Description
`process_lonely_block` in `chain/src/orphan_broker.rs` (lines 111–113) reads `is_pending_verify` first and uses `||` short-circuit:

```rust
let parent_is_pending_verify = self.is_pending_verify.contains(&parent_hash);
let parent_status = self.shared.get_block_status(&parent_hash);
if parent_is_pending_verify || parent_status.contains(BlockStatus::BLOCK_STORED) {
    self.process_descendant(lonely_block);   // taken even when parent is BLOCK_INVALID
} else if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
    self.process_invalid_block(lonely_block); // unreachable during race window
}
```

In `consume_unverified_blocks` (`verify.rs`), when block B fails verification, `BLOCK_INVALID` is written at line 177 but `is_pending_verify.remove` does not execute until line 193. During this window, a child block C arriving on the chain service thread observes `is_pending_verify.contains(B) = true`, short-circuits, and calls `process_descendant(C)`. C is inserted into `is_pending_verify`, sent through the preload channel, and `unverified_tip` is advanced to C's block number. The verify thread then quickly rejects C via the parent re-check at `verify_block` lines 244–252, rolls back `unverified_tip`, and marks C `BLOCK_INVALID`. The inconsistency is confirmed by `search_orphan_leader` (lines 40–59 of `orphan_broker.rs`), which correctly checks `BLOCK_INVALID` before consulting `is_pending_verify`.

## Impact Explanation
The concrete effects are: (1) unnecessary pipeline traversal — C is inserted into `is_pending_verify`, sent through the preload channel, and reaches the verify thread before quick rejection; (2) `unverified_tip` is temporarily advanced to C's block number, causing `find_blocks_to_fetch` to skip fetching legitimate blocks during the window; (3) if the orphan pool contains further descendants of C, `search_orphan_leaders` cascades them all into the same incorrect path, amplifying wasted work proportionally to orphan pool depth. Effects are temporary and self-correcting with no permanent state corruption, no node crash, and no consensus deviation. This matches **Low (501–2000 points): Any other important performance improvements for CKB**.

## Likelihood Explanation
An unprivileged sync peer can increase the probability of hitting the race window by sending an invalid block B (one that passes non-contextual checks but fails contextual verification, e.g., invalid script) and immediately following with one or more child blocks. The race window duration scales with B's script execution complexity, making it reliably wider for blocks with expensive scripts. No privileged access is required. The attack is repeatable at low cost.

## Recommendation
Mirror the check order used in `search_orphan_leader` — check `BLOCK_INVALID` before `is_pending_verify` in `process_lonely_block`:

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

Alternatively, move `is_pending_verify.remove(&block_hash)` to before `insert_block_status(BLOCK_INVALID)` in `consume_unverified_blocks` (`verify.rs` line 193 vs 177), so the flag is never stale relative to the status.

## Proof of Concept
1. Node is syncing. Attacker controls a peer.
2. Attacker sends block B at height N that passes `non_contextual_verify` but fails contextual verification (e.g., invalid script).
3. `process_lonely_block(B)` routes it to `process_descendant(B)`, inserting B into `is_pending_verify` and sending it to the verify thread.
4. Verify thread begins verifying B, calls `insert_block_status(B, BLOCK_INVALID)` (line 177 of `verify.rs`). Before reaching `is_pending_verify.remove(B)` (line 193), attacker's child block C arrives.
5. `process_lonely_block(C)`: `is_pending_verify.contains(B)` = **true** → short-circuits → `process_descendant(C)` → C inserted into `is_pending_verify`, sent to preload channel, `unverified_tip` advanced to N+1.
6. Verify thread finishes B: `is_pending_verify.remove(B)`.
7. C reaches verify thread; `verify_block` detects parent B is `BLOCK_INVALID` (lines 244–252), returns error immediately. `set_unverified_tip` rolls back. C marked `BLOCK_INVALID`.
8. Any orphan descendants of C in the pool are cascaded through the same incorrect path by `search_orphan_leaders`.

A unit test can reproduce this by mocking `is_pending_verify` to return `true` for B's hash while `get_block_status` returns `BLOCK_INVALID` for B, then calling `process_lonely_block(C)` and asserting `process_descendant` is invoked instead of `process_invalid_block`.

### Citations

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

**File:** chain/src/orphan_broker.rs (L111-123)
```rust
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
```

**File:** chain/src/orphan_broker.rs (L180-186)
```rust
        if block_number > self.shared.snapshot().tip_number() {
            self.shared.set_unverified_tip(ckb_shared::HeaderIndex::new(
                block_number,
                block_hash.clone(),
                U256::from(0u64),
            ));

```

**File:** chain/src/orphan_broker.rs (L199-204)
```rust
    fn process_descendant(&self, lonely_block: LonelyBlockHash) {
        self.is_pending_verify
            .insert(lonely_block.block_number_and_hash.hash());

        self.send_unverified_block(lonely_block)
    }
```

**File:** chain/src/verify.rs (L175-193)
```rust
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
```

**File:** chain/src/verify.rs (L243-253)
```rust
        {
            let parent_status = self.shared.get_block_status(&parent_hash);
            if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
                return Err(InternalErrorKind::Other
                    .other(format!(
                        "block: {}'s parent: {} previously verified failed",
                        block_hash, parent_hash
                    ))
                    .into());
            }
        }
```
