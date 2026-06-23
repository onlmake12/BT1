### Title
Incomplete State Cleanup in Block Verification Failure Path Leaves Stale `header_map` Entries — (`chain/src/verify.rs`)

---

### Summary

When a block fails contextual verification in `consume_unverified_blocks`, the `header_map` entry for that block is never removed, while the success path removes it. A secondary instance exists in `process_invalid_block` in `orphan_broker.rs`. This is a direct structural analog to M-02: one code path cleans all associated state, while a parallel path only cleans part of it.

---

### Finding Description

The `ConsumeUnverifiedBlockProcessor::consume_unverified_blocks` function in `chain/src/verify.rs` handles two outcomes after calling `verify_block`:

**Success path** (lines 141–151): removes the block from **both** `block_status_map` and `header_map`:

```rust
Ok(_) => {
    self.shared.remove_block_status(&block_hash);
    self.shared.remove_header_view(&block_hash);
}
``` [1](#0-0) 

**Failure path** (lines 153–190): only inserts `BLOCK_INVALID` into `block_status_map`. It calls `delete_unverified_block` and `insert_block_status`, but **never calls `remove_header_view`**:

```rust
Err(err) => {
    self.delete_unverified_block(&block);
    if !is_internal_db_error(err) {
        self.shared.insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
    } else {
        self.shared.remove_block_status(&block_hash);
    }
    // remove_header_view is never called here
}
``` [2](#0-1) 

The `header_map` entry was created earlier during header sync via `insert_valid_header`, which is called when a peer's header passes validation: [3](#0-2) 

The `get_block_status` function confirms the intended lifecycle: a block in `header_map` but absent from `block_status_map` is returned as `HEADER_VALID`. After full block processing, the entry should be removed from `header_map` regardless of outcome: [4](#0-3) 

A secondary instance of the same pattern exists in `process_invalid_block` in `orphan_broker.rs`, which marks orphan descendants of an invalid block as `BLOCK_INVALID` but does not call `remove_header_view`: [5](#0-4) 

Compare with `clean_expired_orphans`, which correctly calls all three cleanup operations — `delete_block`, `remove_header_view`, and `remove_block_status`: [6](#0-5) 

The codebase itself documents the requirement to keep `orphan_block_pool` and `block_status_map` synchronized; the same invariant applies to `header_map`: [7](#0-6) 

---

### Impact Explanation

Stale `header_map` entries accumulate for every block that passes header validation but fails contextual verification. This causes:

1. **Unbounded memory growth**: Each stale `HeaderIndexView` entry (containing hash, number, epoch, timestamp, parent hash, total difficulty, and skip-list pointers) is never freed. An attacker who can produce valid-PoW headers attached to contextually invalid blocks can drive this growth continuously.

2. **Sync state inconsistency**: `insert_peer_unknown_header_list` queries `header_map` directly to set a peer's best-known header. A stale entry for an invalid block can cause the node to incorrectly record a peer's best chain tip as an invalid block's header index, skewing download scheduling and peer scoring. [8](#0-7) 

3. **Latent status confusion**: If `block_status_map` is ever compacted or the `BLOCK_INVALID` entry is evicted, `get_block_status` would fall through to the `header_map` check and return `HEADER_VALID` for a block that was previously rejected — potentially allowing re-processing of a known-invalid block. [9](#0-8) 

---

### Likelihood Explanation

The primary trigger requires a block whose header passes non-contextual validation (valid PoW, valid structure) but whose full block fails contextual verification (e.g., invalid cell references, script failure, capacity violation). This is reachable by:

- A miner or mining pool that produces a valid-PoW block with invalid transactions.
- A block relayer that forwards a block from a fork where contextual rules differ.
- Any peer that relays a compact block reconstructed with mismatched transactions, causing contextual failure after header acceptance.

The `new_block_received` function confirms that only blocks already in `header_map` (status `HEADER_VALID`) proceed to full verification, so the stale-entry condition is triggered on every contextual verification failure for a previously header-synced block: [10](#0-9) 

---

### Recommendation

In the failure branch of `consume_unverified_blocks`, add a call to `remove_header_view` symmetrically with the success branch:

```rust
Err(err) => {
    self.delete_unverified_block(&block);
    self.shared.remove_header_view(&block_hash); // add this
    if !is_internal_db_error(err) {
        self.shared.insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
    } else {
        self.shared.remove_block_status(&block_hash);
    }
}
```

Apply the same fix to `process_invalid_block` in `orphan_broker.rs` — add `self.shared.remove_header_view(&block_hash)` after `self.delete_block(&lonely_block)`, mirroring what `clean_expired_orphans` already does correctly.

---

### Proof of Concept

1. A peer sends a sequence of headers that pass non-contextual validation (valid PoW, valid parent chain). Each header is inserted into `header_map` via `insert_valid_header`.
2. The peer then sends the corresponding full blocks. Each block passes `non_contextual_verify` in `chain_service.rs` and is stored to DB.
3. During `consume_unverified_blocks`, contextual verification fails (e.g., a transaction spends a non-existent cell).
4. The failure path executes: `delete_unverified_block` removes the block from DB, `insert_block_status(BLOCK_INVALID)` marks it invalid — but `remove_header_view` is never called.
5. Inspecting `shared.header_map()` after step 4 shows the entry for the invalid block's hash still present, while `block_status_map` shows `BLOCK_INVALID`. The two maps are now inconsistent.
6. Repeating steps 1–4 with distinct valid-PoW headers causes `header_map` to grow without bound. [11](#0-10) [5](#0-4)

### Citations

**File:** chain/src/verify.rs (L130-198)
```rust
    pub(crate) fn consume_unverified_blocks(&mut self, unverified_block: UnverifiedBlock) {
        let UnverifiedBlock {
            block,
            switch,
            verify_callback,
            parent_header,
        } = unverified_block;
        let block_hash = block.hash();
        // process this unverified block
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
    }
```

**File:** sync/src/types/mod.rs (L1094-1129)
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
```

**File:** sync/src/types/mod.rs (L1181-1196)
```rust
    pub fn insert_peer_unknown_header_list(&self, pi: PeerIndex, header_list: Vec<Byte32>) {
        // update peer's unknown_header_list only once
        if self.state().peers.unknown_header_list_is_empty(pi) {
            // header list is an ordered list, sorted from highest to lowest,
            // so here you discard and exit early
            for hash in header_list {
                if let Some(header) = self.shared().header_map().get(&hash) {
                    self.state()
                        .peers
                        .may_set_best_known_header(pi, header.as_header_index());
                    break;
                } else {
                    self.state().peers.insert_unknown_header_hash(pi, hash)
                }
            }
        }
```

**File:** sync/src/types/mod.rs (L1200-1227)
```rust
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

**File:** chain/src/orphan_broker.rs (L88-105)
```rust
    fn process_invalid_block(&self, lonely_block: LonelyBlockHash) {
        let block_hash = lonely_block.block_number_and_hash.hash();
        let block_number = lonely_block.block_number_and_hash.number();
        let parent_hash = lonely_block.parent_hash();

        self.delete_block(&lonely_block);

        self.shared
            .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);

        let err: VerifyResult = Err(InternalErrorKind::Other
            .other(format!(
                "parent {} is invalid, so block {}-{} is invalid too",
                parent_hash, block_number, block_hash
            ))
            .into());
        lonely_block.execute_callback(err);
    }
```

**File:** chain/src/orphan_broker.rs (L134-156)
```rust
    pub(crate) fn clean_expired_orphans(&self) {
        debug!("clean expired orphans");
        let tip_epoch_number = self
            .shared
            .store()
            .get_tip_header()
            .expect("tip header")
            .epoch()
            .number();
        let expired_orphans = self
            .orphan_blocks_broker
            .clean_expired_blocks(tip_epoch_number);
        for expired_orphan in expired_orphans {
            self.delete_block(&expired_orphan);
            self.shared.remove_header_view(&expired_orphan.hash());
            self.shared.remove_block_status(&expired_orphan.hash());
            info!(
                "cleaned expired orphan: {}-{}",
                expired_orphan.number(),
                expired_orphan.hash()
            );
        }
    }
```

**File:** chain/src/utils/orphan_block_pool.rs (L125-127)
```rust
// NOTE: Never use `LruCache` as container. We have to ensure synchronizing between
// orphan_block_pool and block_status_map, but `LruCache` would prune old items implicitly.
// RwLock ensures the consistency between maps. Using multiple concurrent maps does not work here.
```
