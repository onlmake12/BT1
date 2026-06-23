### Title
Unbounded Orphan Pool Growth via Epoch Boundary Bypass — (`chain/src/utils/orphan_block_pool.rs`, `chain/src/chain_service.rs`)

### Summary

The orphan block pool has no hard size cap. Its only eviction mechanism is a periodic epoch-based expiry that uses a strict `<` comparison. An attacker can craft orphan blocks whose `epoch_number` is set to exactly `tip_epoch - EXPIRED_EPOCH + 1`, causing the expiry predicate to always evaluate to `false` for those blocks. Combined with the absence of any insertion-time size limit, this allows an unprivileged remote peer to grow the orphan pool (and the underlying DB) without bound, exhausting node memory and disk.

---

### Finding Description

**Expiry predicate — strict `<` creates an off-by-one boundary**

The cleanup condition in `InnerPool::need_clean` is:

```rust
lonely_block.epoch_number() + EXPIRED_EPOCH < tip_epoch
``` [1](#0-0) 

With `EXPIRED_EPOCH = 6`, a block at epoch `tip_epoch - 6 + 1 = tip_epoch - 5` evaluates as:

```
(tip_epoch - 5) + 6 < tip_epoch
tip_epoch + 1 < tip_epoch   →  FALSE  →  never cleaned
``` [2](#0-1) 

**No hard size cap on the pool**

The pool is initialized with `OrphanBlockPool::with_capacity(ORPHAN_BLOCK_SIZE)`, where `ORPHAN_BLOCK_SIZE = BLOCK_DOWNLOAD_WINDOW as usize`. [3](#0-2) 

`with_capacity` is a `HashMap` pre-allocation hint — it is **not** a hard limit. `InnerPool::insert` performs no size check before inserting: [4](#0-3) 

**Cleanup runs only every 60 seconds and is the sole eviction path**

`clean_expired_orphans` is the only mechanism that removes blocks from the pool, and it fires on a 60-second tick: [5](#0-4) 

**Blocks are written to disk before orphan pool insertion**

`asynchronous_process_block` calls `insert_block` (a DB write) before `process_lonely_block` routes the block into the orphan pool: [6](#0-5) 

This means each crafted orphan block consumes both heap memory (pool entry) and disk space (DB row).

---

### Impact Explanation

- **Memory exhaustion**: The `InnerPool` `blocks`, `parents`, and `leaders` maps grow without bound. Each entry holds a `LonelyBlockHash` and associated metadata.
- **Disk exhaustion**: Every accepted orphan is persisted to the chain DB via `insert_block` before pool insertion. Expired blocks that are eventually cleaned call `delete_unverified_block`, but blocks in the safe zone are never deleted.
- **Node crash / DoS**: Sustained flooding causes OOM or disk-full conditions, crashing or stalling the node.

---

### Likelihood Explanation

- The attacker is an unprivileged P2P peer. Sending blocks is a standard sync protocol operation.
- `non_contextual_verify` (`BlockVerifier` + `NonContextualBlockTxsVerifier`) does not validate a block's claimed epoch number against the current chain tip — it is explicitly non-contextual.
- The attacker does not need to eclipse the node. They simply need to continuously send new orphan blocks at `epoch_number = tip_epoch - EXPIRED_EPOCH + 1`. As the tip epoch advances (slowly — CKB epochs are ~4 hours), the attacker adjusts the epoch field of new blocks accordingly. Old blocks in the pool at the previous boundary epoch will then satisfy the expiry condition and be cleaned, but the attacker replaces them with new ones at the new boundary.
- The rate of block production needed to exhaust memory is limited only by the node's network ingress and the cost of constructing minimally valid block headers.

---

### Recommendation

1. **Enforce a hard insertion-time cap**: In `InnerPool::insert`, reject (or evict the oldest entry) when `self.parents.len() >= MAX_ORPHAN_POOL_SIZE`. This is the standard approach used by Bitcoin Core and other implementations.
2. **Change strict `<` to `<=`**: Use `epoch_number + EXPIRED_EPOCH <= tip_epoch` in `need_clean` to eliminate the off-by-one boundary that the attacker exploits.
3. **Per-peer orphan accounting**: Track how many orphans each peer has contributed and apply per-peer limits to prevent a single peer from monopolizing the pool.

---

### Proof of Concept

```
1. Connect to a CKB node as a P2P peer.
2. Read the node's current tip epoch number T.
3. Craft N block headers with epoch_number = T - EXPIRED_EPOCH + 1 = T - 5,
   each with a random unknown parent hash (ensuring they are orphaned).
4. Send each block via the standard block-relay P2P message.
5. Wait 10 × 60 seconds (10 clean cycles).
6. Query orphan_blocks_len() via the sync_state RPC.
7. Assert: pool size ≈ N (not 0), confirming blocks were never evicted.
8. Repeat step 3–4 with increasing N until node OOM-crashes.
``` [7](#0-6) [8](#0-7)

### Citations

**File:** chain/src/utils/orphan_block_pool.rs (L13-13)
```rust
pub const EXPIRED_EPOCH: u64 = 6;
```

**File:** chain/src/utils/orphan_block_pool.rs (L36-54)
```rust
    fn insert(&mut self, lonely_block: LonelyBlockHash) {
        let hash = lonely_block.hash();
        let parent_hash = lonely_block.parent_hash();
        self.blocks
            .entry(parent_hash.clone())
            .or_default()
            .insert(hash.clone(), lonely_block);
        // Out-of-order insertion needs to be deduplicated
        self.leaders.remove(&hash);
        // It is a possible optimization to make the judgment in advance,
        // because the parent of the block must not be equal to its own hash,
        // so we can judge first, which may reduce one arc clone
        if !self.parents.contains_key(&parent_hash) {
            // Block referenced by `parent_hash` is not in the pool,
            // and it has at least one child, the new inserted block, so add it to leaders.
            self.leaders.insert(parent_hash.clone());
        }
        self.parents.insert(hash, parent_hash);
    }
```

**File:** chain/src/utils/orphan_block_pool.rs (L98-122)
```rust
    /// cleanup expired blocks(epoch + EXPIRED_EPOCH < tip_epoch)
    pub fn clean_expired_blocks(&mut self, tip_epoch: EpochNumber) -> Vec<LonelyBlockHash> {
        let mut result = vec![];

        for hash in self.leaders.clone().iter() {
            if self.need_clean(hash, tip_epoch) {
                // remove items in orphan pool and return hash to callee(clean header map)
                let descendants = self.remove_blocks_by_parent(hash);
                result.extend(descendants);
            }
        }
        result
    }

    /// get 1st block belongs to that parent and check if it's expired block
    fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
        self.blocks
            .get(parent_hash)
            .and_then(|map| {
                map.iter().next().map(|(_, lonely_block)| {
                    lonely_block.epoch_number() + EXPIRED_EPOCH < tip_epoch
                })
            })
            .unwrap_or_default()
    }
```

**File:** chain/src/init.rs (L22-43)
```rust
const ORPHAN_BLOCK_SIZE: usize = BLOCK_DOWNLOAD_WINDOW as usize;

/// Here we distinguish between build_chain_services and start_chain_services:
/// * build_chain_services simply initializes ChainController, setting up all relevant
///   threads, and return join handle for the main chain service thread.
/// * start_chain_services first builds relevant data just like build_chain_services,
///   in addition, it register the main chain service thread against CKB's handler. As
///   a result, start_chain_services only returns ChainController, it is expected that
///   CKB's stop handler shall be used to terminate the created chain service.
pub fn start_chain_services(builder: ChainServicesBuilder) -> ChainController {
    let (chain_service, chain_service_thread) = build_chain_services(builder);
    register_thread("ChainService", chain_service_thread);

    chain_service
}

/// Please refer to +start_chain_services+ for difference between build_chain_services
/// and start_chain_services
pub fn build_chain_services(
    builder: ChainServicesBuilder,
) -> (ChainController, thread::JoinHandle<()>) {
    let orphan_blocks_broker = Arc::new(OrphanBlockPool::with_capacity(ORPHAN_BLOCK_SIZE));
```

**File:** chain/src/chain_service.rs (L40-63)
```rust
        let clean_expired_orphan_timer =
            crossbeam::channel::tick(std::time::Duration::from_secs(60));

        loop {
            select! {
                recv(self.process_block_rx) -> msg => match msg {
                    Ok(Request { responder, arguments: lonely_block }) => {
                        // asynchronous_process_block doesn't interact with tx-pool,
                        // no need to pause tx-pool's chunk_process here.
                        let _trace_now = minstant::Instant::now();
                        self.asynchronous_process_block(lonely_block);
                        if let Some(handle) = ckb_metrics::handle(){
                            handle.ckb_chain_async_process_block_duration.observe(_trace_now.elapsed().as_secs_f64())
                        }
                        let _ = responder.send(());
                    },
                    _ => {
                        error!("process_block_receiver closed");
                        break;
                    },
                },
                recv(clean_expired_orphan_timer) -> _ => {
                    self.orphan_broker.clean_expired_orphans();
                },
```

**File:** chain/src/chain_service.rs (L133-143)
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

        self.orphan_broker.process_lonely_block(lonely_block.into());
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
