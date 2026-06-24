Audit Report

## Title
Missing `remove_header_view` in Error Path of `consume_unverified_blocks` Causes Stale Sled Backend Accumulation — (File: chain/src/verify.rs)

## Summary
`ConsumeUnverifiedBlockProcessor::consume_unverified_blocks` calls `remove_header_view` in the `Ok` branch but omits it in the `Err` branch, leaving stale `HeaderIndexView` entries in the `HeaderMap` for every contextually-invalid block. The `HeaderMap` uses a two-tier architecture: a bounded in-memory `MemoryMap` and an unbounded Sled disk backend. While in-memory growth is capped by `memory_limit`, the Sled backend accumulates stale entries without eviction, constituting a suboptimal state storage implementation.

## Finding Description

**Root cause — missing cleanup in error path:**

In `chain/src/verify.rs` lines 140–191, the `Ok` branch removes both `block_status` and `header_view`: [1](#0-0) 

The `Err` branch only updates `block_status` and never calls `remove_header_view`: [2](#0-1) 

**Header view insertion before contextual verification:**

In the relay path, `insert_valid_header` is called after non-contextual checks pass but before full contextual verification in `consume_unverified_blocks`: [3](#0-2) 

**HeaderMap architecture — memory is bounded, Sled backend is not:**

The `HeaderMap` is constructed with a `memory_limit` and a background task running every 5 seconds that calls `limit_memory()`, which evicts excess entries from the in-memory `MemoryMap` into the Sled backend: [4](#0-3) [5](#0-4) 

The Sled backend has no capacity bound. Stale entries evicted from memory accumulate there permanently: [6](#0-5) 

**Correction to the original claim — functional impact is overstated:**

The claim asserts stale header views cause incorrect `get_block_status` results. This is not supported by the code. `get_block_status` checks `block_status_map` first; since `BLOCK_INVALID` is inserted there for failed blocks, the stale header view in the header map is never consulted: [7](#0-6) 

**Cleanup contract is established elsewhere:**

`clean_expired_orphans` removes both `header_view` and `block_status` for expired orphans, confirming the intended cleanup contract: [8](#0-7) 

## Impact Explanation

The actual impact is unbounded disk growth in the Sled backend of the `HeaderMap`, not unbounded RAM growth as claimed. Each contextually-invalid block that passes non-contextual verification leaves a stale `HeaderIndexView` in the Sled backend with no eviction path. This constitutes a suboptimal implementation of CKB state storage mechanism (**Medium, 2001–10000 points**). There is no functional correctness impact on block status queries, sync decisions, or consensus.

## Likelihood Explanation

The attack requires the adversary to produce blocks with **valid proof-of-work** (PoW is part of non-contextual/header verification) that fail contextual verification (e.g., invalid scripts, double-spends, bad DAO fields). This is not a low-effort operation — it requires significant hashpower to generate many distinct valid-PoW blocks. The original claim's characterization of this as trivially triggerable by any peer is inaccurate. The practical likelihood is low, though the bug is structurally real.

## Recommendation

In the `Err` branch of `consume_unverified_blocks`, add a call to `remove_header_view` to mirror the `Ok` branch and the `clean_expired_orphans` cleanup contract:

```rust
Err(err) => {
    // ... existing error handling ...
    self.shared.remove_header_view(&block_hash); // add this
}
``` [2](#0-1) 

## Proof of Concept

1. Connect to a CKB node as a peer with sufficient hashpower.
2. Mine a block with valid PoW and valid structure that fails contextual verification (e.g., a transaction spending a non-existent cell).
3. Send the block via the relay protocol (compact block path), triggering `insert_valid_header` before full verification.
4. `consume_unverified_blocks` runs: contextual verification fails, `BLOCK_INVALID` is set in `block_status_map`, but `remove_header_view` is never called.
5. The `limit_memory` background task eventually evicts the stale entry from the in-memory `MemoryMap` into the Sled backend.
6. Repeat with many distinct block hashes. The Sled backend grows without bound on disk.
7. Confirm by inspecting the Sled backend size and observing it grows proportionally to the number of distinct invalid blocks submitted.

### Citations

**File:** chain/src/verify.rs (L141-151)
```rust
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
```

**File:** chain/src/verify.rs (L153-190)
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

                error!(
                    "set_unverified tip to {}-{}, because verify {} failed: {}",
                    tip.number(),
                    tip.hash(),
                    block_hash,
                    err
                );
            }
```

**File:** sync/src/relayer/compact_block_process.rs (L77-78)
```rust
        // Header has been verified ok, update state
        shared.insert_valid_header(self.peer, &header);
```

**File:** shared/src/types/header_map/mod.rs (L29-53)
```rust
const INTERVAL: Duration = Duration::from_millis(5000);
const ITEM_BYTES_SIZE: usize = size_of::<HeaderIndexView>();
const WARN_THRESHOLD: usize = ITEM_BYTES_SIZE * 100_000;

impl HeaderMap {
    pub fn new<P>(
        tmpdir: Option<P>,
        memory_limit: usize,
        async_handle: &Handle,
        ibd_finished: Arc<AtomicBool>,
    ) -> Self
    where
        P: AsRef<path::Path>,
    {
        if memory_limit < ITEM_BYTES_SIZE {
            panic!("The limit setting is too low");
        }
        if memory_limit < WARN_THRESHOLD {
            ckb_logger::warn!(
                "The low memory limit setting {} will result in inefficient synchronization",
                memory_limit
            );
        }
        let size_limit = memory_limit / ITEM_BYTES_SIZE;
        let inner = Arc::new(HeaderMapKernel::new(tmpdir, size_limit, ibd_finished));
```

**File:** shared/src/types/header_map/kernel_lru.rs (L153-166)
```rust
    pub(crate) fn remove(&self, hash: &Byte32) {
        #[cfg(feature = "stats")]
        {
            self.trace();
            self.stats().tick_primary_delete();
        }
        // If IBD is not finished, don't shrink memory map
        let allow_shrink_to_fit = self.ibd_finished.load(Ordering::Acquire);
        self.memory.remove(hash, allow_shrink_to_fit);
        if self.backend.is_empty() {
            return;
        }
        self.backend.remove_no_return(hash);
    }
```

**File:** shared/src/types/header_map/kernel_lru.rs (L168-182)
```rust
    pub(crate) fn limit_memory(&self) {
        let _trace_timer: Option<HistogramTimer> = ckb_metrics::handle()
            .map(|handle| handle.ckb_header_map_limit_memory_duration.start_timer());

        if let Some(values) = self.memory.front_n(self.memory_limit) {
            tokio::task::block_in_place(|| {
                self.backend.insert_batch(&values);
            });

            // If IBD is not finished, don't shrink memory map
            let allow_shrink_to_fit = self.ibd_finished.load(Ordering::Acquire);
            self.memory
                .remove_batch(values.iter().map(|value| value.hash()), allow_shrink_to_fit);
        }
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
