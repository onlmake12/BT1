### Title
TOCTOU Race in `process_lonely_block` / `process_descendant` Causes Preload Thread Panic and Node Block-Processing Halt — (`chain/src/orphan_broker.rs`, `chain/src/preload_unverified_blocks_channel.rs`)

---

### Summary

A time-of-check/time-of-use (TOCTOU) race between the `ChainService` thread (running `OrphanBroker`) and the `verify_blocks` thread (running `ConsumeUnverifiedBlockProcessor`) allows a child block C to be dispatched to the preload thread with a parent P whose DB header entry has already been deleted due to P's failed contextual verification. The preload thread calls `.expect("parent header stored")` on the missing header, panics unconditionally, and terminates. This cascades into the verify thread also terminating, permanently halting block processing on the node.

---

### Finding Description

**Threading model.** Three threads are relevant:

| Thread | Struct | Shared state |
|---|---|---|
| `ChainService` | `OrphanBroker` | `is_pending_verify: Arc<DashSet<Byte32>>` |
| `verify_blocks` | `ConsumeUnverifiedBlockProcessor` | same `Arc<DashSet<Byte32>>` |
| `preload_unverified_block` | `PreloadUnverifiedBlocksChannel` | `shared: Shared` (same RocksDB store) |

**The TOCTOU window.** In `process_lonely_block`, the ChainService thread reads `is_pending_verify` and then unconditionally forwards C to the preload channel — with no lock held across the two operations:

```
line 111: let parent_is_pending_verify = self.is_pending_verify.contains(&parent_hash);
line 113: if parent_is_pending_verify || ...  {
line 118:     self.process_descendant(lonely_block);   // inserts C, sends to preload channel
``` [1](#0-0) 

`process_descendant` inserts C into `is_pending_verify` and sends it to the preload channel: [2](#0-1) 

**Concurrent failure path on the verify thread.** When P fails contextual verification, `consume_unverified_blocks` (on the `verify_blocks` thread) does, in order:

1. Calls `self.delete_unverified_block(&block)` — which commits a RocksDB transaction that deletes `COLUMN_BLOCK_HEADER` for P's hash.
2. Calls `self.is_pending_verify.remove(&block_hash)`. [3](#0-2) 

`delete_block` explicitly removes `COLUMN_BLOCK_HEADER`: [4](#0-3) 

**The panic site.** The preload thread calls `load_full_unverified_block_by_hash(C)`, which reads P's header from the same RocksDB column:

```rust
self.shared
    .store()
    .get_block_header(&parent_hash)
    .expect("parent header stored")   // ← panics if P was deleted
``` [5](#0-4) 

`get_block_header` reads directly from `COLUMN_BLOCK_HEADER` with no fallback: [6](#0-5) 

The preload thread's `start()` loop has **no `catch_unwind` wrapper** around `preload_unverified_channel`: [7](#0-6) 

(Contrast: the `verify_blocks` thread does wrap `consume_unverified_blocks` in `catch_unwind` at `verify.rs:86-96`, but the preload thread does not.)

**Cascade.** When the preload thread panics:
- `PreloadUnverifiedBlocksChannel` is dropped → `unverified_block_tx` (the sender to the verify thread) is dropped.
- `ConsumeUnverifiedBlocks.start()` receives `Err` on `unverified_block_rx` and returns, terminating the verify thread.
- The node can no longer verify any block. The `ChainService` thread continues running but `send_unverified_block` silently discards all future blocks once the preload receiver is gone. [8](#0-7) 

---

### Impact Explanation

The node permanently stops processing new blocks. From the outside it appears live (P2P connections remain, RPC responds) but the chain tip is frozen. This is a complete block-processing denial-of-service. Recovery requires a node restart.

---

### Likelihood Explanation

The race window is wide: P's contextual verification (CKB-VM script execution, transaction resolution) can take hundreds of milliseconds to seconds for a block with complex scripts, while the ChainService thread dispatches C to the preload channel in microseconds. An attacker who mines a PoW-valid block P containing a contextually invalid transaction (e.g., a script that always fails, a double-spend, or an invalid capacity) and then immediately sends child C can reliably hit this window. Solving PoW for a single block is a real cost but does not require majority hashpower, and the attack can be attempted repeatedly.

---

### Recommendation

1. **Add `catch_unwind` in the preload thread loop** (mirroring the pattern already used in `verify_blocks`) so a panic on one task does not kill the thread.
2. **Replace `.expect("parent header stored")` with graceful error handling** — if the parent header is absent, log an error and drop the task (C will be re-queued or discarded as invalid once P's `BLOCK_INVALID` status propagates).
3. **Atomically gate dispatch on parent validity** — after the `is_pending_verify.contains` check, re-check `get_block_status(&parent_hash)` immediately before sending to the preload channel, so a concurrently-failed parent is detected before C is forwarded.

---

### Proof of Concept

```
1. Attacker mines block P (valid PoW, invalid contextual content — e.g., script always aborts).
2. Attacker sends P to the victim node via P2P SendBlock.
   → P passes non-contextual checks, is stored in DB, inserted into is_pending_verify,
     sent to preload channel, then to verify channel.
3. Attacker immediately sends child C (parent_hash = P.hash) via P2P SendBlock.
   → C passes non-contextual checks, is stored in DB.
   → OrphanBroker.process_lonely_block(C): is_pending_verify.contains(P) == true
     → process_descendant(C) → C sent to preload channel.
4. Concurrently, verify_blocks thread finishes verifying P:
   → P fails → delete_unverified_block(P) commits → COLUMN_BLOCK_HEADER[P.hash] deleted
   → is_pending_verify.remove(P)
5. Preload thread dequeues C:
   → load_full_unverified_block_by_hash(C)
   → get_block_header(P.hash) → None
   → .expect("parent header stored") → PANIC
6. Preload thread terminates → unverified_block_tx dropped → verify thread terminates.
7. Node tip is frozen; no further blocks are processed until restart.
```

### Citations

**File:** chain/src/orphan_broker.rs (L111-118)
```rust
        let parent_is_pending_verify = self.is_pending_verify.contains(&parent_hash);
        let parent_status = self.shared.get_block_status(&parent_hash);
        if parent_is_pending_verify || parent_status.contains(BlockStatus::BLOCK_STORED) {
            debug!(
                "parent {} has stored: {:?} or is_pending_verify: {}, processing descendant directly {}-{}",
                parent_hash, parent_status, parent_is_pending_verify, block_number, block_hash,
            );
            self.process_descendant(lonely_block);
```

**File:** chain/src/orphan_broker.rs (L168-179)
```rust
        match self.preload_unverified_tx.send(lonely_block) {
            Ok(_) => {
                debug!(
                    "process desendant block success {}-{}",
                    block_number, block_hash
                );
            }
            Err(_) => {
                info!("send unverified_block_tx failed, the receiver has been closed");
                return;
            }
        };
```

**File:** chain/src/orphan_broker.rs (L199-204)
```rust
    fn process_descendant(&self, lonely_block: LonelyBlockHash) {
        self.is_pending_verify
            .insert(lonely_block.block_number_and_hash.hash());

        self.send_unverified_block(lonely_block)
    }
```

**File:** chain/src/verify.rs (L153-193)
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
        }

        self.is_pending_verify.remove(&block_hash);
```

**File:** store/src/transaction.rs (L213-217)
```rust
    pub fn delete_block(&self, block: &BlockView) -> Result<(), Error> {
        let hash = block.hash();
        let txs_len = block.transactions().len();
        self.delete(COLUMN_BLOCK_HEADER, hash.as_slice())?;
        self.delete(COLUMN_BLOCK_UNCLE, hash.as_slice())?;
```

**File:** chain/src/preload_unverified_blocks_channel.rs (L33-51)
```rust
    pub(crate) fn start(&self) {
        loop {
            select! {
                recv(self.preload_unverified_rx) -> msg => match msg {
                    Ok(preload_unverified_block_task) =>{
                        self.preload_unverified_channel(preload_unverified_block_task);
                    },
                    Err(_err) =>{
                        info!("recv preload_task_rx failed");
                        break;
                    }
                },
                recv(self.stop_rx) -> _ => {
                    info!("preload_unverified_blocks thread received exit signal, exit now");
                    break;
                }
            }
        }
    }
```

**File:** chain/src/preload_unverified_blocks_channel.rs (L91-96)
```rust
        let parent_header = {
            self.shared
                .store()
                .get_block_header(&parent_hash)
                .expect("parent header stored")
        };
```

**File:** store/src/store.rs (L73-91)
```rust
    fn get_block_header(&self, hash: &packed::Byte32) -> Option<HeaderView> {
        if let Some(cache) = self.cache()
            && let Some(header) = cache.headers.lock().get(hash)
        {
            return Some(header.clone());
        };
        let ret = self.get(COLUMN_BLOCK_HEADER, hash.as_slice()).map(|slice| {
            let reader = packed::HeaderViewReader::from_slice_should_be_ok(slice.as_ref());
            Into::<HeaderView>::into(reader)
        });

        if let Some(cache) = self.cache() {
            ret.inspect(|header| {
                cache.headers.lock().put(hash.clone(), header.clone());
            })
        } else {
            ret
        }
    }
```
