The code is fully readable. Let me analyze the exact path.

The full code path is now clear. Here is the analysis:

---

### Title
Unbounded Wait Loop in `read_from_json` Causes Permanent CLI Hang When Importing a Block with `number = u64::MAX` — (`util/instrument/src/import.rs`)

### Summary

`Import::read_from_json` updates `largest_block_number` unconditionally when a block is dequeued from the channel, then enters a boundless polling loop waiting for `snapshot.get_block_hash(largest_block_number)` to return `Some`. A crafted JSONL file containing a single block whose `number` field is `0xffffffffffffffff` sets `largest_block_number` to `u64::MAX`. Because that block's parent can never exist in the chain, it is permanently parked in the orphan pool and never committed, so `get_block_hash(u64::MAX)` returns `None` forever and the process never exits.

### Finding Description

**Step 1 — `largest_block_number` is set unconditionally.** [1](#0-0) 

`largest_block_number` is updated at line 192 for every non-genesis block that arrives on the channel, regardless of whether the chain will ever accept it.

**Step 2 — The block is dispatched asynchronously.** [2](#0-1) 

`asynchronous_process_lonely_block` fires-and-forgets; the caller receives no success/failure signal.

**Step 3 — Chain service rejects the block silently (orphan pool).** [3](#0-2) 

After optional non-contextual verification, the block is inserted into the DB and handed to `OrphanBroker::process_lonely_block`. Because the parent of a block numbered `u64::MAX` is not stored and not pending-verify, the block is placed in the orphan pool and its callback is never invoked with a committed result. [4](#0-3) 

**Step 4 — The wait loop has no exit condition for failure.** [5](#0-4) 

`get_block_hash` is a plain DB index lookup: [6](#0-5) 

No block at height `u64::MAX` is ever written to `COLUMN_INDEX`, so the lookup returns `None` on every iteration and the loop spins at 1-second intervals indefinitely.

**Step 5 — Orphan cleanup does not help.**

The orphan pool is cleaned every 60 seconds for *expired* blocks (epoch-based TTL): [7](#0-6) [8](#0-7) 

Even after the orphan is evicted, `get_block_hash(u64::MAX)` still returns `None` — the wait condition is never satisfied.

### Impact Explanation

The `ckb import` process hangs permanently. It consumes a thread sleeping in a 1-second loop and holds all chain-service threads open. The only recovery is `SIGKILL`. Any operator who runs `ckb import` against a crafted file (or whose import pipeline is fed attacker-controlled data) is affected.

### Likelihood Explanation

The attack requires only the ability to invoke `ckb import` with a crafted JSONL file. No cryptographic material, no network access, no elevated privileges, and no PoW solution are needed (with `--skip-all-verify`; even without it, a structurally valid block with a bogus parent hash passes non-contextual checks and still ends up in the orphan pool). The file can be as small as two lines (genesis + the malicious block).

### Recommendation

Replace the unconditional `largest_block_number` update with tracking only blocks that are successfully committed, **or** add a deadline/timeout to the wait loop, **or** break out of the loop when the callback signals a permanent failure (e.g., `BLOCK_INVALID` status or orphan eviction). A minimal fix:

```rust
// After the for loop, only wait if largest_block_number > 0
// and add a timeout / error channel fed by the verify_callback.
```

### Proof of Concept

```jsonl
{"header":{"version":"0x0","compact_target":"0x207fffff","timestamp":"0x...","number":"0x0",...},...}  # genesis
{"header":{"version":"0x0","compact_target":"0x207fffff","timestamp":"0x...","number":"0xffffffffffffffff","parent_hash":"0xdeadbeef..."},...}
```

```bash
ckb import --skip-all-verify crafted.jsonl
# Process sleeps in the while loop at import.rs:224 forever;
# `kill -9` is the only way to terminate it.
```

### Citations

**File:** util/instrument/src/import.rs (L188-192)
```rust
        for (block, block_size) in blocks_rx {
            if !block.is_genesis() {
                use ckb_chain::LonelyBlock;

                largest_block_number = largest_block_number.max(block.number());
```

**File:** util/instrument/src/import.rs (L215-220)
```rust
                let lonely_block = LonelyBlock {
                    block,
                    switch: Some(self.switch),
                    verify_callback: Some(callback),
                };
                self.chain.asynchronous_process_lonely_block(lonely_block);
```

**File:** util/instrument/src/import.rs (L224-231)
```rust
        while self
            .shared
            .snapshot()
            .get_block_hash(largest_block_number)
            .is_none()
        {
            std::thread::sleep(std::time::Duration::from_secs(1));
        }
```

**File:** chain/src/chain_service.rs (L40-41)
```rust
        let clean_expired_orphan_timer =
            crossbeam::channel::tick(std::time::Duration::from_secs(60));
```

**File:** chain/src/chain_service.rs (L92-143)
```rust
    fn asynchronous_process_block(&self, lonely_block: LonelyBlock) {
        let block_number = lonely_block.block().number();
        let block_hash = lonely_block.block().hash();
        // Skip verifying a genesis block if its hash is equal to our genesis hash,
        // otherwise, return error and ban peer.
        if block_number < 1 {
            if self.shared.genesis_hash() != block_hash {
                warn!(
                    "receive 0 number block: 0-{}, expect genesis hash: {}",
                    block_hash,
                    self.shared.genesis_hash()
                );
                self.shared
                    .insert_block_status(lonely_block.block().hash(), BlockStatus::BLOCK_INVALID);
                let error = InternalErrorKind::System
                    .other("Invalid genesis block received")
                    .into();
                lonely_block.execute_callback(Err(error));
            } else {
                warn!("receive 0 number block: 0-{}", block_hash);
                lonely_block.execute_callback(Ok(false));
            }
            return;
        }

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

**File:** chain/src/orphan_broker.rs (L107-123)
```rust
    pub(crate) fn process_lonely_block(&self, lonely_block: LonelyBlockHash) {
        let block_hash = lonely_block.block_number_and_hash.hash();
        let block_number = lonely_block.block_number_and_hash.number();
        let parent_hash = lonely_block.parent_hash();
        let parent_is_pending_verify = self.is_pending_verify.contains(&parent_hash);
        let parent_status = self.shared.get_block_status(&parent_hash);
        if parent_is_pending_verify || parent_status.contains(BlockStatus::BLOCK_STORED) {
            debug!(
                "parent {} has stored: {:?} or is_pending_verify: {}, processing descendant directly {}-{}",
                parent_hash, parent_status, parent_is_pending_verify, block_number, block_hash,
            );
            self.process_descendant(lonely_block);
        } else if parent_status.eq(&BlockStatus::BLOCK_INVALID) {
            self.process_invalid_block(lonely_block);
        } else {
            self.orphan_blocks_broker.insert(lonely_block);
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

**File:** store/src/store.rs (L266-270)
```rust
    fn get_block_hash(&self, number: BlockNumber) -> Option<packed::Byte32> {
        let block_number: packed::Uint64 = number.into();
        self.get(COLUMN_INDEX, block_number.as_slice())
            .map(|raw| packed::Byte32Reader::from_slice_should_be_ok(raw.as_ref()).to_entity())
    }
```
