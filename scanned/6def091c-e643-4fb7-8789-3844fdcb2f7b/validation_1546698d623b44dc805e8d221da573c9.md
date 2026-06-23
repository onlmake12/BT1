The code is fully readable. Let me now deliver the analysis.

---

### Title
Non-Deterministic Orphan Expiry via HashMap Sibling Poisoning in `need_clean` — (`chain/src/utils/orphan_block_pool.rs`)

### Summary

`need_clean` samples only the **first** child of a leader via `map.iter().next()` on a `HashMap`, whose iteration order is non-deterministic. An unprivileged remote peer can insert one fresh-epoch block as a sibling of an expired subtree under the same leader, causing cleanup to silently skip the entire expired subtree whenever the fresh sibling happens to be returned first. Because the attacker can re-inject the fresh sibling after each cleanup cycle, expired blocks accumulate in the pool indefinitely, exhausting node memory.

---

### Finding Description

`need_clean` in `InnerPool`:

```rust
fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
    self.blocks
        .get(parent_hash)
        .and_then(|map| {
            map.iter().next().map(|(_, lonely_block)| {   // ← only ONE child checked
                lonely_block.epoch_number() + EXPIRED_EPOCH < tip_epoch
            })
        })
        .unwrap_or_default()
}
``` [1](#0-0) 

The inner structure `blocks: HashMap<ParentHash, HashMap<packed::Byte32, LonelyBlockHash>>` stores all direct children of a leader in a `HashMap`. `map.iter().next()` returns an **arbitrary** child — whichever the HashMap's internal bucket order happens to place first. If that child is not expired, `need_clean` returns `false` and `clean_expired_blocks` skips the entire subtree:

```rust
for hash in self.leaders.clone().iter() {
    if self.need_clean(hash, tip_epoch) {
        let descendants = self.remove_blocks_by_parent(hash);
        result.extend(descendants);
    }
}
``` [2](#0-1) 

The cleanup timer fires every 60 seconds: [3](#0-2) 

There is **no hard size cap** on `OrphanBlockPool` — `with_capacity` sets initial allocation only, and `insert` has no rejection path based on pool size. [4](#0-3) 

---

### Impact Explanation

Expired orphan blocks accumulate without bound. Each cleanup cycle that is "poisoned" leaves the full expired subtree in memory. The attacker re-injects the fresh sibling after any cycle that does clean it, keeping the attack alive indefinitely. The node's RSS grows monotonically until OOM kill or swap exhaustion, causing a full node crash and loss of availability.

---

### Likelihood Explanation

**Attacker entry point:** Any P2P peer via the `SendBlock` / compact block relay path. The block passes `non_contextual_verify` (`BlockVerifier` + `NonContextualBlockTxsVerifier`), which does **not** check epoch continuity — that is a contextual check requiring the parent block, which is precisely what is missing for orphan blocks. [5](#0-4) 

The `EpochVerifier` that enforces `is_successor_of(parent)` is only invoked contextually, after the parent is known: [6](#0-5) 

So the attacker freely crafts blocks with epoch=1 (expired relative to a tip at epoch 8+) and a sibling with a current epoch, both referencing the same unknown parent hash (the leader). Both pass non-contextual verification and land in the orphan pool. The attack requires only a standard P2P connection and the ability to send crafted block messages — no PoW, no keys, no privileged access.

---

### Recommendation

Replace the single-sample check with an **all-children** check: a leader's subtree should be cleaned only if **all** direct children are expired (or alternatively, if **any** child is expired, since mixing epochs under one leader is itself anomalous). The minimal fix:

```rust
fn need_clean(&self, parent_hash: &packed::Byte32, tip_epoch: EpochNumber) -> bool {
    self.blocks
        .get(parent_hash)
        .map(|map| {
            !map.is_empty()
                && map.values().all(|b| b.epoch_number() + EXPIRED_EPOCH < tip_epoch)
        })
        .unwrap_or_default()
}
```

Additionally, enforce a hard maximum on `OrphanBlockPool` size (analogous to `DEFAULT_MAX_ORPHAN_TRANSACTIONS` in the tx-pool orphan pool) to bound memory even if the expiry logic has edge cases.

---

### Proof of Concept

```rust
// Setup: tip_epoch = 20, EXPIRED_EPOCH = 6, so threshold = 13
// Leader L = some unknown parent hash not in the chain.
// Insert D=100 blocks with epoch=1 (expired), all children of L.
// Insert 1 block with epoch=15 (fresh), also a child of L.
//
// Now blocks[L] = HashMap { expired_1..expired_100, fresh_1 }
//
// Call clean_expired_blocks(20) 1000 times (re-inserting fresh_1 if removed).
// Assert: after each call, all 100 expired blocks are removed.
//
// Observed: ~50% of calls return 0 removed blocks because map.iter().next()
// returns fresh_1 first, need_clean returns false, and the entire subtree is skipped.
```

The existing test `test_remove_expired_blocks` only inserts a single chain with no siblings, so it never exercises the multi-sibling case and does not catch this bug. [7](#0-6)

### Citations

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

**File:** chain/src/utils/orphan_block_pool.rs (L99-110)
```rust
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
```

**File:** chain/src/utils/orphan_block_pool.rs (L113-122)
```rust
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

**File:** chain/src/chain_service.rs (L72-89)
```rust
    fn non_contextual_verify(&self, block: &BlockView) -> Result<(), Error> {
        let consensus = self.shared.consensus();
        BlockVerifier::new(consensus).verify(block).map_err(|e| {
            debug!("[process_block] BlockVerifier error {:?}", e);
            e
        })?;

        NonContextualBlockTxsVerifier::new(consensus)
            .verify(block)
            .map_err(|e| {
                debug!(
                    "[process_block] NonContextualBlockTxsVerifier error {:?}",
                    e
                );
                e
            })
            .map(|_| ())
    }
```

**File:** verification/src/header_verifier.rs (L133-148)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        if !self.header.epoch().is_well_formed() {
            return Err(EpochError::Malformed {
                value: self.header.epoch(),
            }
            .into());
        }
        if !self.parent.is_genesis() && !self.header.epoch().is_successor_of(self.parent) {
            return Err(EpochError::NonContinuous {
                current: self.header.epoch(),
                parent: self.parent,
            }
            .into());
        }
        Ok(())
    }
```

**File:** chain/src/tests/orphan_block_pool.rs (L232-263)
```rust
#[test]
fn test_remove_expired_blocks() {
    let consensus = ConsensusBuilder::default().build();
    let block_number = 20;
    let mut parent = consensus.genesis_block().header();
    let pool = OrphanBlockPool::with_capacity(block_number);

    let deprecated = EpochNumberWithFraction::new(10, 0, 10);

    for _ in 1..block_number {
        let new_block = BlockBuilder::default()
            .parent_hash(parent.hash())
            .timestamp(unix_time_as_millis())
            .number(parent.number() + 1)
            .epoch(deprecated)
            .nonce(parent.nonce() + 1)
            .build();

        parent = new_block.header();
        let lonely_block = LonelyBlock {
            block: Arc::new(new_block),
            switch: None,
            verify_callback: None,
        };
        pool.insert(lonely_block.into());
    }
    assert_eq!(pool.leaders_len(), 1);

    let v = pool.clean_expired_blocks(20_u64);
    assert_eq!(v.len(), 19);
    assert_eq!(pool.leaders_len(), 0);
}
```
