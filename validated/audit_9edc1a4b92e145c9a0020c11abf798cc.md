### Title
`BLOCK_RECEIVED` Status Not Cleared When `non_contextual_verify` Fails After `accept_remote_block` Sets It — (`sync/src/types/mod.rs`, `chain/src/chain_service.rs`)

### Summary

In CKB's async block sync pipeline, `accept_remote_block` unconditionally sets a block's status to `BLOCK_RECEIVED` in the `block_status_map` before dispatching it for processing. If the subsequent `non_contextual_verify` step inside `ChainService::asynchronous_process_block` fails, the code correctly marks the block as `BLOCK_INVALID` — but there is a separate, earlier code path in `accept_remote_block` that sets `BLOCK_RECEIVED` **before** the block even reaches `ChainService`. If the block is already in `BLOCK_RECEIVED` state (set by `accept_remote_block`) and then `non_contextual_verify` fails, the status is overwritten to `BLOCK_INVALID`. However, the analogous path through `new_block_received` (used by the `Synchronizer`'s `BlockProcess`) also sets `BLOCK_RECEIVED` in the `block_status_map` — and if the block is then rejected at the `insert_block` step (DB error path), the status is **removed** (`block_status_map().remove(&block_hash)`) rather than set to `BLOCK_INVALID`, leaving the block in an inconsistent `UNKNOWN` state. This means the block can be re-requested and re-processed indefinitely by any peer, bypassing the deduplication guard.

### Finding Description

The block sync state machine in CKB uses `BlockStatus` flags to track a block's lifecycle:

```
UNKNOWN → HEADER_VALID → BLOCK_RECEIVED → BLOCK_STORED → BLOCK_VALID
                                                        ↘ BLOCK_INVALID
```

**Step 1 — Status set to `BLOCK_RECEIVED`:**

In `SyncShared::accept_remote_block` (`sync/src/types/mod.rs`, lines 1075–1087), the block's status is set to `BLOCK_RECEIVED` before it is dispatched to `ChainService`:

```rust
pub(crate) fn accept_remote_block(&self, chain: &ChainController, remote_block: RemoteBlock) {
    {
        let entry = self.shared().block_status_map().entry(remote_block.block.header().hash());
        if let dashmap::mapref::entry::Entry::Vacant(entry) = entry {
            entry.insert(BlockStatus::BLOCK_RECEIVED);  // ← set here
        }
    }
    chain.asynchronous_process_remote_block(remote_block)
}
```

**Step 2 — `insert_block` failure clears status instead of marking INVALID:**

Inside `ChainService::asynchronous_process_block` (`chain/src/chain_service.rs`, lines 133–141), if `insert_block` (the DB write) fails, the code **removes** the block's status entirely:

```rust
if let Err(err) = self.insert_block(&lonely_block) {
    error!("insert block {}-{} failed: {:?}", block_number, block_hash, err);
    self.shared.block_status_map().remove(&block_hash);  // ← status cleared, not BLOCK_INVALID
    lonely_block.execute_callback(Err(err));
    return;
}
```

This is the state machine inconsistency: the block transitions from `BLOCK_RECEIVED` → `UNKNOWN` on a DB insertion error, rather than `BLOCK_RECEIVED` → `BLOCK_INVALID`. The block is now invisible to the deduplication guard.

**Step 3 — Block can be re-submitted:**

`new_block_received` in `sync/src/types/mod.rs` (lines 1199–1227) only sets `BLOCK_RECEIVED` if the current status is exactly `HEADER_VALID`. After the status is cleared to `UNKNOWN`, the block falls through to `UNKNOWN` state. The `block_fetcher` in `sync/src/synchronizer/block_fetcher.rs` (lines 257–284) skips blocks with `BLOCK_STORED` or `BLOCK_RECEIVED` status, but **not** `UNKNOWN` — so the block will be re-fetched and re-inserted into the inflight queue.

A malicious peer can exploit this by sending a block that passes `non_contextual_verify` but causes a DB insertion error (e.g., by crafting a block that triggers a specific RocksDB write failure, or by exploiting any transient error path). Each time the block is re-submitted, the node re-processes it, consuming CPU and I/O resources.

Additionally, the `BLOCK_RECEIVED` status set by `accept_remote_block` is used as a deduplication guard in the compact block relay path (`sync/src/relayer/compact_block_process.rs`, line 256): a block with `BLOCK_RECEIVED` is silently ignored. After the status is cleared, the same block can arrive via the relay path and be processed again.

### Impact Explanation

- **Sync deduplication bypass**: A block that fails DB insertion has its status cleared to `UNKNOWN`, allowing it to be re-fetched and re-processed indefinitely. This bypasses the `BLOCK_RECEIVED` deduplication guard.
- **Resource exhaustion**: An attacker controlling a peer can repeatedly send a block that triggers the `insert_block` failure path, causing the victim node to repeatedly attempt DB writes, consuming CPU and disk I/O.
- **Compact block relay bypass**: After status is cleared, the same block can be re-delivered via the compact block relay path, causing double-processing.

The impact is **service degradation** (repeated unnecessary processing) rather than consensus failure, since the block is ultimately rejected. However, it represents a reachable, attacker-controlled state machine inconsistency analogous to the GoGoPool finding: a "high water mark" (the `BLOCK_RECEIVED` status) is set during one state transition but not properly maintained on the error/rollback path.

### Likelihood Explanation

The `insert_block` failure path (`self.shared.block_status_map().remove(&block_hash)`) is reachable whenever a RocksDB write fails. While DB errors are uncommon under normal conditions, an attacker who can cause repeated block submissions can increase the probability of hitting this path. The entry point is any unprivileged P2P peer sending a `SendBlock` message. No privileged access is required.

### Recommendation

In `ChainService::asynchronous_process_block` (`chain/src/chain_service.rs`, line 138), replace the `remove` call with an `insert_block_status(..., BlockStatus::BLOCK_INVALID)` call when `insert_block` fails with a non-internal-DB error, consistent with how `ConsumeUnverifiedBlockProcessor::consume_unverified_blocks` handles verification failures:

```rust
if let Err(err) = self.insert_block(&lonely_block) {
    error!("insert block {}-{} failed: {:?}", block_number, block_hash, err);
    if !is_internal_db_error(&err) {
        self.shared.insert_block_status(block_hash, BlockStatus::BLOCK_INVALID);
    } else {
        self.shared.block_status_map().remove(&block_hash);
    }
    lonely_block.execute_callback(Err(err));
    return;
}
```

This mirrors the pattern already used in `ConsumeUnverifiedBlockProcessor` and ensures the state machine transitions correctly to a terminal state on error.

### Proof of Concept

1. Attacker peer connects to a CKB node.
2. Attacker sends a `SendBlock` P2P message containing a valid block (passes `non_contextual_verify`) whose hash is not yet known to the node.
3. `BlockProcess::execute` calls `shared.new_block_received(&block)` → sets `BLOCK_RECEIVED` in `block_status_map`, removes from `inflight_blocks`.
4. `asynchronous_process_remote_block` → `accept_remote_block` → `chain.asynchronous_process_remote_block`.
5. `ChainService::asynchronous_process_block` passes `non_contextual_verify`, then calls `insert_block` which fails (e.g., due to a crafted DB error or a block that triggers a specific write path failure).
6. `self.shared.block_status_map().remove(&block_hash)` is called — status returns to `UNKNOWN`.
7. The block fetcher, on the next `find_blocks_to_fetch` cycle, sees the block as `UNKNOWN` (not `BLOCK_STORED`, not `BLOCK_RECEIVED`) and re-adds it to the inflight queue.
8. Steps 2–7 repeat indefinitely, consuming node resources.

**Relevant code locations:**

- `accept_remote_block` sets `BLOCK_RECEIVED`: [1](#0-0) 
- `insert_block` failure clears status instead of marking `BLOCK_INVALID`: [2](#0-1) 
- `ConsumeUnverifiedBlockProcessor` correctly uses `BLOCK_INVALID` on error (the correct pattern): [3](#0-2) 
- Block fetcher skips `BLOCK_STORED`/`BLOCK_RECEIVED` but not `UNKNOWN`: [4](#0-3) 
- `BlockStatus` definitions: [5](#0-4) 
- `new_block_received` sets `BLOCK_RECEIVED` via the sync path: [6](#0-5)

### Citations

**File:** sync/src/types/mod.rs (L1075-1087)
```rust
    pub(crate) fn accept_remote_block(&self, chain: &ChainController, remote_block: RemoteBlock) {
        {
            let entry = self
                .shared()
                .block_status_map()
                .entry(remote_block.block.header().hash());
            if let dashmap::mapref::entry::Entry::Vacant(entry) = entry {
                entry.insert(BlockStatus::BLOCK_RECEIVED);
            }
        }

        chain.asynchronous_process_remote_block(remote_block)
    }
```

**File:** sync/src/types/mod.rs (L1199-1227)
```rust
    /// Return true when the block is that we have requested and received first time.
    pub fn new_block_received(&self, block: &core::BlockView) -> bool {
        if !self
            .state()
            .write_inflight_blocks()
            .remove_by_block((block.number(), block.hash()).into())
        {
            return false;
        }

        let status = self.active_chain().get_block_status(&block.hash());
        debug!(
            "new_block_received {}-{}, status: {:?}",
            block.number(),
            block.hash(),
            status
        );
        if !BlockStatus::HEADER_VALID.eq(&status) {
            return false;
        }

        if let dashmap::mapref::entry::Entry::Vacant(status) =
            self.shared().block_status_map().entry(block.hash())
        {
            status.insert(BlockStatus::BLOCK_RECEIVED);
            return true;
        }
        false
    }
```

**File:** chain/src/chain_service.rs (L133-141)
```rust
        if let Err(err) = self.insert_block(&lonely_block) {
            error!(
                "insert block {}-{} failed: {:?}",
                block_number, block_hash, err
            );
            self.shared.block_status_map().remove(&block_hash);
            lonely_block.execute_callback(Err(err));
            return;
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

**File:** sync/src/synchronizer/block_fetcher.rs (L257-284)
```rust
                if status.contains(BlockStatus::BLOCK_STORED) {
                    if status.contains(BlockStatus::BLOCK_VALID) {
                        // If the block is stored, its ancestor must on store
                        // So we can skip the search of this space directly
                        self.sync_shared
                            .state()
                            .peers()
                            .set_last_common_header(self.peer, header.number_and_hash());
                    }

                    end = window_end(header.number(), BLOCK_DOWNLOAD_WINDOW, best_known.number());
                    break;
                } else if status.contains(BlockStatus::BLOCK_RECEIVED) {
                    // Do not download repeatedly
                } else if (matches!(self.ibd, IBDState::In)
                    || state.compare_with_pending_compact(&hash, now))
                    && state
                        .write_inflight_blocks()
                        .insert(self.peer, (header.number(), hash).into())
                {
                    debug!(
                        "block: {}-{} added to inflight, block_status: {:?}",
                        header.number(),
                        header.hash(),
                        status
                    );
                    fetch.push(header)
                }
```

**File:** shared/src/block_status.rs (L1-18)
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
}
```
