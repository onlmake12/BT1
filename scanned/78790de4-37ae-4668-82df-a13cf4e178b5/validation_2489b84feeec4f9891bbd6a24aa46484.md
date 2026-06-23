The code is fully traceable. Here is the analysis:

---

## Code Path Verification

**Step 1 — The `.expect()` call exists exactly as described.**

`chain/src/preload_unverified_blocks_channel.rs:91-96`:
```rust
let parent_header = {
    self.shared
        .store()
        .get_block_header(&parent_hash)
        .expect("parent header stored")
};
``` [1](#0-0) 

**Step 2 — `delete_block` explicitly deletes `COLUMN_BLOCK_HEADER`.**

`store/src/transaction.rs:213-238`:
```rust
pub fn delete_block(&self, block: &BlockView) -> Result<(), Error> {
    let hash = block.hash();
    let txs_len = block.transactions().len();
    self.delete(COLUMN_BLOCK_HEADER, hash.as_slice())?;  // header is gone
    ...
}
``` [2](#0-1) 

**Step 3 — `delete_unverified_block` calls `db_txn.delete_block(&block)` on verification failure.**

`chain/src/lib.rs:200-210` and `chain/src/verify.rs:173`: [3](#0-2) [4](#0-3) 

**Step 4 — The verify thread has `catch_unwind`; the preload thread does not.**

Verify thread (`verify.rs:86-96`) wraps `consume_unverified_blocks` in `catch_unwind`. The preload thread's `start()` loop has no such protection — a panic propagates directly out of `preload_unverified_channel` → `start()`, killing the thread. [5](#0-4) [6](#0-5) 

**Step 5 — The race window is real.**

In `orphan_broker.rs`, C is enqueued to the preload channel when P is in `is_pending_verify` (line 52-53, 113). The preload channel is bounded (`BLOCK_DOWNLOAD_WINDOW*10`). The verify thread removes P from `is_pending_verify` and calls `delete_unverified_block(P)` (verify.rs:173, 193) before the preload thread dequeues C. After deletion, `get_block_header(&P.hash())` returns `None` → `.expect()` panics. [7](#0-6) [8](#0-7) 

**Step 6 — Downstream impact of preload thread crash.**

If the preload thread crashes, its `unverified_block_tx` sender is dropped. The verify thread's `unverified_block_rx` returns `Err`, causing it to `return` (verify.rs:103-106), permanently halting all block verification. [9](#0-8) 

---

## Attacker Entry Point Constraint

For P to reach the preload channel, it must pass non-contextual validation (including PoW) and be stored in the DB. P must then **fail contextual verification** (e.g., invalid transactions, invalid epoch, etc.) to trigger `delete_unverified_block`. This requires the attacker to mine a block with valid PoW but invalid contextual content — a meaningful but not majority-hashpower barrier. A single block mine suffices; no 51% attack is needed.

---

## Verdict

### Title
Unguarded `.expect("parent header stored")` in Preload Thread Causes Permanent Node Halt via Race with Verify Thread Deleting Parent Block — (`chain/src/preload_unverified_blocks_channel.rs`)

### Summary
A race between the verify thread deleting a failed parent block's header from `COLUMN_BLOCK_HEADER` and the preload thread calling `.expect("parent header stored")` for a child block causes an unhandled panic that crashes the preload thread and permanently halts all block verification.

### Finding Description
The three-stage pipeline is: orphan broker → preload channel → verify channel. When parent P and child C are both enqueued in the preload channel (C because P was in `is_pending_verify`), the following race is possible:

1. Preload thread dequeues P, sends it to the verify channel.
2. Verify thread processes P; P fails contextual verification.
3. Verify thread calls `delete_unverified_block(P)` → `StoreTransaction::delete_block` → deletes `COLUMN_BLOCK_HEADER` for P's hash.
4. Preload thread dequeues C, calls `store().get_block_header(&P.hash()).expect("parent header stored")` → `None` → **panic**.
5. No `catch_unwind` in the preload thread; the thread dies.
6. The verify thread's `unverified_block_rx` returns `Err`; the verify thread exits.

### Impact Explanation
Permanent halt of all block verification on the targeted node. The node continues to run but cannot advance its chain tip, effectively making it useless as a full node.

### Likelihood Explanation
Requires mining one block with valid PoW but invalid contextual content (e.g., a transaction spending a non-existent cell). This is feasible for any miner. The race window is wide because the preload channel is bounded at `BLOCK_DOWNLOAD_WINDOW*10` items, giving the verify thread ample time to delete P before the preload thread reaches C.

### Recommendation
Replace the `.expect()` with graceful error handling in `load_full_unverified_block_by_hash`. If `get_block_header` returns `None`, log an error and skip the block (or send an error callback). Additionally, add `catch_unwind` to the preload thread's main loop, mirroring the verify thread's existing protection.

### Proof of Concept
1. Mine block P (valid PoW, invalid transaction — e.g., double-spend).
2. Send P to the target node via P2P sync; P is stored in DB and enters `is_pending_verify`.
3. Immediately send child block C (parent = P) to the target node; C is enqueued in the preload channel because P is in `is_pending_verify`.
4. The verify thread processes P, fails, calls `delete_unverified_block(P)` removing P's header from `COLUMN_BLOCK_HEADER`.
5. The preload thread processes C, calls `.expect("parent header stored")` on the now-deleted header → panic → preload thread exits → verify thread exits → node halts block verification.

### Citations

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

**File:** store/src/transaction.rs (L213-238)
```rust
    pub fn delete_block(&self, block: &BlockView) -> Result<(), Error> {
        let hash = block.hash();
        let txs_len = block.transactions().len();
        self.delete(COLUMN_BLOCK_HEADER, hash.as_slice())?;
        self.delete(COLUMN_BLOCK_UNCLE, hash.as_slice())?;
        self.delete(COLUMN_BLOCK_EXTENSION, hash.as_slice())?;
        self.delete(COLUMN_BLOCK_PROPOSAL_IDS, hash.as_slice())?;
        self.delete(
            COLUMN_NUMBER_HASH,
            packed::NumberHash::new_builder()
                .number(block.number())
                .block_hash(hash.clone())
                .build()
                .as_slice(),
        )?;
        // currently rocksdb transaction do not support `DeleteRange`
        // https://github.com/facebook/rocksdb/issues/4812
        for index in 0..txs_len {
            let key = packed::TransactionKey::new_builder()
                .block_hash(hash.clone())
                .index(index)
                .build();
            self.delete(COLUMN_BLOCK_BODY, key.as_slice())?;
        }
        Ok(())
    }
```

**File:** chain/src/lib.rs (L200-210)
```rust
    let db_txn = store.begin_transaction();
    let block_op: Option<BlockView> = db_txn.get_block(&block_hash);
    match block_op {
        Some(block) => {
            if let Err(err) = db_txn.delete_block(&block) {
                error!(
                    "delete block {}-{} failed {:?}",
                    block_number, block_hash, err
                );
                return;
            }
```

**File:** chain/src/verify.rs (L86-96)
```rust
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
```

**File:** chain/src/verify.rs (L103-106)
```rust
                    Err(err) => {
                        error!("unverified_block_rx err: {}", err);
                        return;
                    },
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

**File:** chain/src/orphan_broker.rs (L52-59)
```rust
        let leader_is_pending_verify = self.is_pending_verify.contains(&leader_hash);
        if !leader_is_pending_verify && !leader_status.contains(BlockStatus::BLOCK_STORED) {
            trace!(
                "orphan leader: {} not stored {:?} and not in is_pending_verify: {}",
                leader_hash, leader_status, leader_is_pending_verify
            );
            return;
        }
```
