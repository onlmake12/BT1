### Title
`consume_unverified_blocks` Cleans `header_view` on Success but Not on Contextual Verification Failure — (`chain/src/verify.rs`)

### Summary

`ConsumeUnverifiedBlockProcessor::consume_unverified_blocks` removes both `block_status` and `header_view` from shared in-memory state when a block passes full contextual verification, but on the failure path it only updates `block_status` to `BLOCK_INVALID` and never calls `remove_header_view`. This leaves stale header views for contextually-invalid blocks permanently in the header map, analogous to how `transferERC721` omits the cleanup that `timeUnlockERC721` performs.

---

### Finding Description

In `chain/src/verify.rs`, `consume_unverified_blocks` handles the result of full block verification:

```rust
match &verify_result {
    Ok(_) => {
        self.shared.remove_block_status(&block_hash);   // cleaned
        self.shared.remove_header_view(&block_hash);    // cleaned
    }
    Err(err) => {
        // ...
        self.delete_unverified_block(&block);
        if !is_internal_db_error(err) {
            self.shared.insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
        } else {
            self.shared.remove_block_status(&block_hash);
        }
        // remove_header_view is NEVER called here
    }
}
self.is_pending_verify.remove(&block_hash);
``` [1](#0-0) 

The header view is inserted into the shared header map earlier in the pipeline — for example, via `shared.insert_valid_header` in the relay path — before the block undergoes full contextual verification. [2](#0-1) 

The cleanup contract is clear from `clean_expired_orphans`, which explicitly removes **both** `header_view` and `block_status` for expired orphan blocks:

```rust
self.shared.remove_header_view(&expired_orphan.hash());
self.shared.remove_block_status(&expired_orphan.hash());
``` [3](#0-2) 

The `remove_header_view` and `remove_block_status` functions operate on the shared in-memory maps: [4](#0-3) 

---

### Impact Explanation

An unprivileged peer can send blocks that pass non-contextual verification (structure, size, proposal count) but fail contextual verification (script execution, cell availability, DAO fields, uncle validity). Each such block causes a stale `header_view` entry to accumulate in the shared header map with no automatic eviction path.

The header map is a `DashMap` without a fixed capacity bound. Repeated delivery of contextually-invalid blocks causes unbounded memory growth in the header map. Additionally, stale header views for invalid blocks may be consulted during sync decisions — for example, `get_header_index_view` is called in `contextual_check` to determine whether a compact block's parent is known: [5](#0-4) 

A stale header view for an invalid block could cause the node to skip the `CompactBlockRequiresParent` guard for a child of an invalid block, leading to wasted processing.

---

### Likelihood Explanation

Any peer connected to the node can trigger this by sending a block that:
1. Has valid structure (passes `BlockVerifier` and `NonContextualBlockTxsVerifier`)
2. Fails contextual verification (e.g., invalid script, double-spend, bad DAO field)

This is a standard, low-effort operation for any network peer. No privileged access, key material, or majority hashpower is required. The `asynchronous_process_block` path accepts such blocks and forwards them to the unverified pipeline: [6](#0-5) 

---

### Recommendation

In the `Err` branch of `consume_unverified_blocks`, add a call to `remove_header_view` to mirror the cleanup performed in the `Ok` branch:

```rust
Err(err) => {
    // ... existing error handling ...
    self.shared.remove_header_view(&block_hash); // add this
}
``` [7](#0-6) 

---

### Proof of Concept

1. Connect to a CKB node as a peer.
2. Construct a block that passes `BlockVerifier` and `NonContextualBlockTxsVerifier` (valid header, valid structure, valid proposal count) but fails contextual verification (e.g., a transaction spending a non-existent cell, or an invalid script).
3. Send the block via the sync protocol (`SendBlock` message).
4. The block passes `non_contextual_verify`, is stored as unverified, and its header view is inserted into the shared header map.
5. `consume_unverified_blocks` runs, contextual verification fails, `block_status` is set to `BLOCK_INVALID`, but `remove_header_view` is never called.
6. Repeat with many distinct block hashes. The header map grows without bound, consuming node memory. [8](#0-7)

### Citations

**File:** chain/src/verify.rs (L129-198)
```rust
impl ConsumeUnverifiedBlockProcessor {
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

**File:** sync/src/relayer/compact_block_process.rs (L77-78)
```rust
        // Header has been verified ok, update state
        shared.insert_valid_header(self.peer, &header);
```

**File:** sync/src/relayer/compact_block_process.rs (L263-280)
```rust
    let store_first = tip.number() + 1 >= compact_block_header.number();
    let parent = shared.get_header_index_view(
        &compact_block_header.data().raw().parent_hash(),
        store_first,
    );
    if parent.is_none() {
        debug_target!(
            crate::LOG_TARGET_RELAY,
            "UnknownParent: {}, send_getheaders_to_peer({})",
            block_hash,
            peer
        );
        active_chain.send_getheaders_to_peer(nc, peer, (&tip).into());
        return StatusCode::CompactBlockRequiresParent.with_context(format!(
            "{} parent: {}",
            block_hash,
            compact_block_header.data().raw().parent_hash(),
        ));
```

**File:** chain/src/orphan_broker.rs (L146-155)
```rust
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
```

**File:** shared/src/shared.rs (L459-468)
```rust
    pub fn remove_block_status(&self, block_hash: &Byte32) {
        let log_now = std::time::Instant::now();
        self.block_status_map.remove(block_hash);
        debug!("remove_block_status cost {:?}", log_now.elapsed());
        shrink_to_fit!(self.block_status_map, SHRINK_THRESHOLD);
        debug!(
            "remove_block_status shrink_to_fit cost {:?}",
            log_now.elapsed()
        );
    }
```

**File:** chain/src/chain_service.rs (L117-143)
```rust
        if lonely_block.switch().is_none()
            || matches!(lonely_block.switch(), Some(switch) if !switch.disable_non_contextual())
        {
            let result = self.non_contextual_verify(lonely_block.block());
            if let Err(err) = result {
                error!(
                    "block {}-{} verify failed: {:?}",
                    block_number, block_hash, err
                );
                self.shared
                    .insert_block_status(lonely_block.block().hash(), BlockStatus::BLOCK_INVALID);
                lonely_block.execute_callback(Err(err));
                return;
            }
        }

        if let Err(err) = self.insert_block(&lonely_block) {
            error!(
                "insert block {}-{} failed: {:?}",
                block_number, block_hash, err
            );
            self.shared.block_status_map().remove(&block_hash);
            lonely_block.execute_callback(Err(err));
            return;
        }

        self.orphan_broker.process_lonely_block(lonely_block.into());
```
