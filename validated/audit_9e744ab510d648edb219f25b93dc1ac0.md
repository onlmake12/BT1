### Title
`SyncState::shared_best_header` Never Downgraded After Header Removal or Block Invalidation — (`sync/src/types/mod.rs`)

---

### Summary

`SyncState::shared_best_header` is a monotonically increasing global pointer that tracks the best-known header across all peers. It is updated only upward via `may_set_shared_best_header()`. When the header it points to is subsequently removed from `header_map` (because the corresponding block fails full verification) or when an orphan block expires and its header is evicted, `shared_best_header` is **never downgraded**. This is the direct CKB analog of the reported queue `tail` pointer not being updated on node removal: a secondary index that becomes stale after the primary entry is deleted.

---

### Finding Description

**Root cause — `may_set_shared_best_header` is write-only upward:** [1](#0-0) 

The function only allows the pointer to advance to a higher total difficulty. There is no corresponding "downgrade" path.

**Path 1 — block body fails verification:**

When `ConsumeUnverifiedBlockProcessor::consume_unverified_blocks()` processes a block whose body is invalid, it removes the header from `header_map` and marks the block `BLOCK_INVALID`, but never touches `shared_best_header`: [2](#0-1) 

`remove_header_view` removes the entry from `header_map`: [3](#0-2) 

`shared_best_header` is left pointing to the now-removed, now-invalid header.

**Path 2 — expired orphan eviction:**

`clean_expired_orphans()` removes orphan blocks and their header map entries, again without touching `shared_best_header`: [4](#0-3) 

**Where the stale pointer is consumed:**

`min_chain_work_ready()` reads `shared_best_header` to decide whether block download may begin during IBD: [5](#0-4) 

`BlockFetchCMD::can_start()` uses this to gate the transition from `MinWorkNotReach` to `AssumeValidNotFound`/`Ready`: [6](#0-5) 

The stale `shared_best_header` also feeds the `sync_state` RPC, which exposes it to external callers: [7](#0-6) 

**The header is set only after full header validation (PoW included):** [8](#0-7) [9](#0-8) 

---

### Impact Explanation

A stale `shared_best_header` pointing to a removed or invalid header causes:

1. **Premature `min_chain_work_ready()` = `true`**: The node believes it has seen a chain meeting the minimum work threshold when it has not. During IBD this gates the entire block-download pipeline (`BlockFetchCMD::can_start()`), so the node may begin downloading blocks from peers before it has actually observed a sufficiently-worked honest chain.

2. **Incorrect `assume_valid_target` logic**: The `can_start()` path uses `shared_best_header_ref().timestamp()` to decide whether to skip the assume-valid target check. A stale high-difficulty, recent-timestamp header can cause the node to abandon the assume-valid target and fall back to full verification from genesis, or conversely to skip verification it should perform.

3. **Misleading RPC output**: `sync_state` RPC returns `best_known_block_number` and `best_known_block_timestamp` derived from `shared_best_header`. Operators and tooling relying on this to assess sync progress receive incorrect data.

The impact does **not** affect consensus correctness — blocks are still individually verified before being committed to the chain. The impact is on sync-state machine correctness and IBD gating.

---

### Likelihood Explanation

Exploiting Path 1 requires a peer that can produce headers with valid PoW but invalid block bodies (e.g., a miner including invalid transactions). Even a miner with a small fraction of hashpower can produce such blocks. The attacker:

1. Mines a block with valid PoW but an invalid body (e.g., a double-spend or invalid script).
2. Sends the header to the victim via `SendHeaders`; the victim validates the header (PoW passes) and advances `shared_best_header`.
3. Sends or allows the victim to request the block body; the body fails `consume_unverified_blocks`.
4. `shared_best_header` is now permanently stale for the lifetime of the node process.

Path 2 (orphan expiry) requires only sending orphan blocks whose headers pass PoW validation; after 6 epochs the orphan is evicted and the stale pointer remains.

---

### Recommendation

Add a downgrade path to `SyncState`. When `remove_header_view` is called (on block invalidation or orphan expiry), check whether `shared_best_header` points to the removed hash and, if so, recompute it from the current per-peer `best_known_header` values or fall back to the committed chain tip's `HeaderIndex`. Alternatively, make `shared_best_header` a derived value recomputed from live peer state rather than a monotonically-advancing cached pointer.

---

### Proof of Concept

1. Start a CKB node in IBD mode with a non-zero `min_chain_work`.
2. Connect a malicious peer that sends `SendHeaders` containing a chain of headers with valid PoW and total difficulty exceeding `min_chain_work`. The victim calls `insert_valid_header` → `may_set_shared_best_header`; `shared_best_header` is now set to the attacker's high-difficulty header.
3. The victim requests the block bodies. The malicious peer sends block bodies with invalid transactions. `consume_unverified_blocks` marks them `BLOCK_INVALID` and calls `remove_header_view`, but does **not** update `shared_best_header`.
4. Call the `sync_state` RPC: `best_known_block_number` and `best_known_block_timestamp` reflect the now-invalid chain. `min_chain_work_ready()` returns `true` even though no valid chain meeting `min_chain_work` has been observed. Block download proceeds against the wrong chain tip reference.

### Citations

**File:** sync/src/types/mod.rs (L1129-1140)
```rust
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
```

**File:** sync/src/types/mod.rs (L1348-1352)
```rust
    pub fn min_chain_work_ready(&self) -> bool {
        self.shared_best_header
            .read()
            .is_better_than(&self.min_chain_work)
    }
```

**File:** sync/src/types/mod.rs (L1398-1408)
```rust
    pub fn may_set_shared_best_header(&self, header: HeaderIndexView) {
        let mut shared_best_header = self.shared_best_header.write();
        if !header.is_better_than(shared_best_header.total_difficulty()) {
            return;
        }

        if let Some(metrics) = ckb_metrics::handle() {
            metrics.ckb_shared_best_number.set(header.number() as i64);
        }
        *shared_best_header = header;
    }
```

**File:** chain/src/verify.rs (L143-181)
```rust
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
```

**File:** shared/src/shared.rs (L417-419)
```rust
    pub fn remove_header_view(&self, hash: &Byte32) {
        self.header_map.remove(hash);
    }
```

**File:** chain/src/orphan_broker.rs (L134-155)
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
```

**File:** sync/src/synchronizer/mod.rs (L260-264)
```rust
        let min_work_reach = |flag: &mut CanStart| {
            if state.min_chain_work_ready() {
                *flag = CanStart::AssumeValidNotFound;
            }
        };
```

**File:** rpc/src/module/net.rs (L734-756)
```rust
        let best_known = state.shared_best_header();
        let min_chain_work = {
            let mut min_chain_work_500k_u128: [u8; 16] = [0; 16];
            min_chain_work_500k_u128
                .copy_from_slice(&self.sync_shared.state().min_chain_work().to_le_bytes()[..16]);
            u128::from_le_bytes(min_chain_work_500k_u128)
        };
        let unverified_tip = shared.get_unverified_tip();
        let sync_state = SyncState {
            ibd: chain.is_initial_block_download(),
            assume_valid_target_reached: shared.assume_valid_targets().is_none(),
            assume_valid_target: Into::<packed::Byte32>::into(
                shared
                    .assume_valid_target_specified()
                    .as_ref()
                    .clone()
                    .unwrap_or_default(),
            )
            .into(),
            min_chain_work: min_chain_work.into(),
            min_chain_work_reached: state.min_chain_work_ready(),
            best_known_block_number: best_known.number().into(),
            best_known_block_timestamp: best_known.timestamp().into(),
```

**File:** sync/src/synchronizer/headers_process.rs (L324-357)
```rust
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
```
