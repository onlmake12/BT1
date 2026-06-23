### Title
Incomplete Panic Cleanup in Block Verification Loop Leaves Unverified Block Persistent Without `BLOCK_INVALID` Status — (`chain/src/verify.rs`)

---

### Summary

The `ConsumeUnverifiedBlocks::start()` loop wraps `consume_unverified_blocks` in a `catch_unwind` to survive panics. However, the panic branch only removes the block from `is_pending_verify` and does **not** execute the three cleanup steps that the normal error path performs: resetting `unverified_tip`, deleting the block from the persistent unverified store, and stamping the block `BLOCK_INVALID`. A block that panics the verifier therefore persists in the unverified store with no invalid marker, and can be re-relayed and re-processed.

---

### Finding Description

`ConsumeUnverifiedBlocks::start()` drives the main block-verification loop:

```
chain/src/verify.rs  lines 71-126
``` [1](#0-0) 

When `consume_unverified_blocks` panics, the handler executes only:

```rust
self.processor.is_pending_verify.remove(&block_hash);
```

The **normal error path** inside `consume_unverified_blocks` performs three additional, security-critical cleanup steps that the panic branch skips entirely:

| Step | Code location | Effect |
|---|---|---|
| Reset `unverified_tip` | line 167 | Rolls the unverified chain tip back to the last verified tip |
| Delete from unverified store | line 173 | Removes the block from persistent storage |
| Mark `BLOCK_INVALID` | line 177 | Prevents the block from ever being re-accepted | [2](#0-1) 

The panic handler performs **none** of these: [1](#0-0) 

Concrete panic surfaces inside `consume_unverified_blocks` / `verify_block` that are reachable from attacker-supplied block data include:

- `self.shared.consensus().next_epoch_ext(...).expect("epoch should be stored")` — panics if epoch data is absent for the block's parent.
- `self.shared.store().get_tip_header().expect("tip_header must exist")` — panics on unexpected DB state reached after a verification error.
- `self.shared.store().get_block_ext(&tip.hash()).expect("tip header's ext must exist")` — same. [3](#0-2) [4](#0-3) 

Additionally, `is_internal_db_error` itself deliberately panics on `DataCorrupted`: [5](#0-4) 

---

### Impact Explanation

After a panic the following inconsistencies persist until the node restarts:

1. **Persistent storage leak** — the block remains in the unverified block store because `delete_unverified_block` was never called.
2. **No `BLOCK_INVALID` stamp** — the block can be re-relayed by any peer and re-queued for verification. If the panic was caused by a transient condition (race, temporary DB state), the block may succeed on re-submission and be accepted into the canonical chain without having passed a complete verification cycle.
3. **`unverified_tip` not rolled back** — the node continues to advertise a chain tip that was never successfully verified, corrupting its sync state and potentially misleading peers about the best chain.

The combination of (2) and (3) is the direct CKB analog of the GMX "Delay Limit Success" pattern: a block that should have been permanently rejected instead remains in the store and can be re-executed when conditions are favorable.

---

### Likelihood Explanation

Entry path: an unprivileged **block/header relayer** (listed in scope). A peer relays a crafted block whose parent epoch data is absent or whose DB-side invariants are violated, triggering one of the `expect()` panics in `verify_block`. The `catch_unwind` in `start()` absorbs the panic, the loop continues, but the three cleanup steps are skipped. The block remains in the unverified store without an invalid marker. The same peer (or any other) can immediately re-relay the block.

Likelihood is **moderate**: the `expect()` calls are reachable from block content, the loop is designed to survive panics (confirming the developers anticipated this scenario), and the missing cleanup is a straightforward omission rather than a design choice.

---

### Recommendation

Move all three cleanup operations into the outer `catch_unwind` handler, or introduce a RAII drop-guard at the top of `consume_unverified_blocks` that performs the cleanup unconditionally (on both normal return and unwind). The guard should:

1. Call `delete_unverified_block` for the block.
2. Call `insert_block_status(block_hash, BLOCK_INVALID)`.
3. Call `set_unverified_tip` to roll back to the last verified tip.

This mirrors the pattern already used for `is_pending_verify.remove()` in the panic branch.

---

### Proof of Concept

1. Attacker (block relayer) crafts a block `B` whose parent's epoch data is absent from the node's store, or whose verification triggers one of the `expect()` panics in `verify_block`.
2. The block is relayed to the victim node and enqueued in `unverified_block_rx`.
3. `ConsumeUnverifiedBlocks::start()` dequeues `B` and calls `consume_unverified_blocks`.
4. `verify_block` panics (e.g., at `next_epoch_ext(...).expect("epoch should be stored")`).
5. `catch_unwind` catches the panic; only `is_pending_verify.remove(&block_hash)` executes.
6. `delete_unverified_block` is **not** called → `B` remains in the persistent unverified store.
7. `insert_block_status(BLOCK_INVALID)` is **not** called → `B` has no invalid marker.
8. `set_unverified_tip` is **not** called → `unverified_tip` still points to `B`.
9. The attacker re-relays `B`; the node re-queues it (no `BLOCK_INVALID` guard blocks it).
10. If the transient condition that caused the panic is resolved, `B` is now verified and attached to the chain — having bypassed the full verification cycle on its first attempt. [6](#0-5) [7](#0-6)

### Citations

**File:** chain/src/verify.rs (L71-126)
```rust
    pub(crate) fn start(mut self) {
        loop {
            let trace_begin_loop = minstant::Instant::now();
            select! {
                recv(self.unverified_block_rx) -> msg => match msg {
                    Ok(unverified_task) => {
                        // process this unverified block
                        if let Some(handle) = ckb_metrics::handle() {
                            handle.ckb_chain_consume_unverified_block_waiting_block_duration.observe(trace_begin_loop.elapsed().as_secs_f64())
                        }
                        let _ = self.tx_pool_controller.suspend_chunk_process();

                        let _trace_now = minstant::Instant::now();
                        let block_hash = unverified_task.block.hash();
                        let block_number = unverified_task.block.number();
                        if let Err(payload) = catch_unwind(AssertUnwindSafe(|| {
                            self.processor.consume_unverified_blocks(unverified_task);
                        })) {
                            error!(
                                "consume unverified block {}-{} panicked: {}",
                                block_number,
                                block_hash,
                                panic_payload_to_string(payload.as_ref())
                            );
                            self.processor.is_pending_verify.remove(&block_hash);
                        }
                        if let Some(handle) = ckb_metrics::handle() {
                            handle.ckb_chain_consume_unverified_block_duration.observe(_trace_now.elapsed().as_secs_f64())
                        }

                        let _ = self.tx_pool_controller.continue_chunk_process();
                    },
                    Err(err) => {
                        error!("unverified_block_rx err: {}", err);
                        return;
                    },
                },
                recv(self.truncate_block_rx) -> msg => match msg {
                    Ok(Request { responder, arguments: target_tip_hash }) => {
                        let _ = self.tx_pool_controller.suspend_chunk_process();
                        let _ = responder.send(self.processor.truncate(&target_tip_hash));
                        let _ = self.tx_pool_controller.continue_chunk_process();
                    },
                    Err(err) => {
                        info!("truncate_block_tx has been closed, err: {}", err);
                        return;
                    },
                },
                recv(self.stop_rx) -> _ => {
                    info!("verify_blocks thread received exit signal, exit now");
                    break;
                }

            }
        }
    }
```

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

**File:** chain/src/verify.rs (L310-315)
```rust
        let next_block_epoch = self
            .shared
            .consensus()
            .next_epoch_ext(parent_header, &self.shared.store().borrow_as_data_loader())
            .expect("epoch should be stored");
        let new_epoch = next_block_epoch.is_head();
```

**File:** error/src/lib.rs (L101-114)
```rust
pub fn is_internal_db_error(error: &Error) -> bool {
    if error.kind() == ErrorKind::Internal {
        let error_kind = error
            .downcast_ref::<InternalError>()
            .expect("error kind checked")
            .kind();
        if error_kind == InternalErrorKind::DataCorrupted {
            panic!("{}", error)
        } else {
            return error_kind == InternalErrorKind::Database
                || error_kind == InternalErrorKind::System;
        }
    }
    false
```
