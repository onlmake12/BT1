Audit Report

## Title
Unbounded Wait Loop in `read_from_json` Causes Permanent CLI Hang When Importing a Block with `number = u64::MAX` — (`util/instrument/src/import.rs`)

## Summary

`Import::read_from_json` unconditionally sets `largest_block_number` to the maximum block number seen from the channel before dispatching blocks to the chain service. After all blocks are dispatched, it enters a boundless polling loop waiting for `snapshot.get_block_hash(largest_block_number)` to return `Some`. A crafted JSONL file containing a block with `number = 0xffffffffffffffff` sets `largest_block_number` to `u64::MAX`. Because no failure path in the chain service or orphan broker ever writes `COLUMN_INDEX` for that height, `get_block_hash(u64::MAX)` returns `None` forever and the process never exits.

## Finding Description

**Root cause — `largest_block_number` updated unconditionally before dispatch, with no rollback on failure.**

At `util/instrument/src/import.rs:192`, for every non-genesis block dequeued from the channel, `largest_block_number` is updated before the block is handed to the chain service:

```rust
largest_block_number = largest_block_number.max(block.number());
``` [1](#0-0) 

There is no mechanism to reset this value if the block is later rejected.

**Block is dispatched fire-and-forget; callback only prints on failure.**

`asynchronous_process_lonely_block` returns immediately. The verify callback writes nothing to any shared variable the wait loop checks — it only prints to stderr on error: [2](#0-1) 

**The wait loop has no exit condition for failure.**

After the channel drains, the loop at L224–231 polls `get_block_hash(largest_block_number)` with a 1-second sleep and no deadline, timeout, or failure flag: [3](#0-2) 

**Chain service: two failure paths, neither satisfies the wait condition.**

*Path A — non-contextual verification fails (without `--skip-all-verify`):* The callback is invoked with `Err` at `chain_service.rs:128`, the block is never inserted into `COLUMN_INDEX`, and `get_block_hash(u64::MAX)` remains `None`. The wait loop is not unblocked. [4](#0-3) 

*Path B — verification skipped or passes, block inserted into DB, then orphaned:* Because the parent of a `u64::MAX`-numbered block is not stored and not pending-verify, `process_lonely_block` falls into the `else` branch and inserts the block into the orphan pool. The callback is never invoked with a committed result, and `COLUMN_INDEX` is never written for height `u64::MAX`. [5](#0-4) 

**`get_block_hash` is a plain `COLUMN_INDEX` lookup.**

No block at height `u64::MAX` is ever written to `COLUMN_INDEX`, so every iteration of the wait loop returns `None`: [6](#0-5) 

**Orphan cleanup does not help.**

Even after `clean_expired_orphans` evicts the block from the orphan pool, `get_block_hash(u64::MAX)` still returns `None` — the wait condition is never satisfied: [7](#0-6) 

## Impact Explanation

The `ckb import` CLI process hangs permanently, consuming a thread sleeping in a 1-second loop and holding all chain-service threads open. The only recovery is `SIGKILL`. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local command line crash.**

## Likelihood Explanation

The attack requires only the ability to supply a crafted JSONL file to `ckb import`. With `--skip-all-verify`, no PoW solution or valid block structure is needed. Even without that flag, the hang is triggered regardless of whether non-contextual verification passes or fails: if it fails, the callback is called with `Err` but the wait loop is still not unblocked (Path A). If it passes (block crafted with `since = u64::MAX` in the cellbase input to satisfy `CellbaseVerifier` at L150, valid merkle root, etc.), the block ends up in the orphan pool (Path B). The file can be as small as two lines (genesis + the malicious block). No cryptographic material, network access, or elevated privileges are required. [8](#0-7) 

## Recommendation

Replace the unconditional `largest_block_number` update with tracking only blocks that are successfully committed via callback, **or** add a deadline/timeout to the wait loop, **or** feed a shared atomic/channel from the verify callback so the loop can exit on permanent failure:

```rust
// Option 1: add a timeout
let deadline = std::time::Instant::now() + std::time::Duration::from_secs(MAX_WAIT_SECS);
while self.shared.snapshot().get_block_hash(largest_block_number).is_none() {
    if std::time::Instant::now() > deadline {
        return Err(Box::new(io::Error::other("timed out waiting for block commit")));
    }
    std::thread::sleep(std::time::Duration::from_secs(1));
}

// Option 2: track largest_block_number only on successful commit via callback
// Use an Arc<AtomicU64> updated inside the callback on Ok(_), and poll that instead.
```

## Proof of Concept

```jsonl
{"header":{"version":"0x0","compact_target":"0x207fffff","timestamp":"0x...","number":"0x0",...},...}
{"header":{"version":"0x0","compact_target":"0x207fffff","timestamp":"0x...","number":"0xffffffffffffffff","parent_hash":"0xdeadbeef..."},...}
```

```bash
ckb import --skip-all-verify crafted.jsonl
# Process enters the while loop at import.rs:224 and sleeps forever;
# `kill -9` is the only way to terminate it.
```

The genesis block satisfies the first-block parent check at L105–133. The `u64::MAX` block is then dispatched, `largest_block_number` is set to `u64::MAX`, and after the channel drains the process enters the infinite wait loop. With `--skip-all-verify` no valid PoW or merkle root is required. [9](#0-8)

### Citations

**File:** util/instrument/src/import.rs (L105-134)
```rust
        if !first_block.is_genesis() {
            let first_block_parent = first_block.parent_hash();
            if self
                .shared
                .snapshot()
                .get_block(&first_block_parent)
                .is_none()
            {
                let tip = self
                    .shared
                    .snapshot()
                    .get_tip_header()
                    .expect("must get tip header");

                let source_display = match self.source {
                    ImportSource::Path(ref path) => path.display().to_string(),
                    ImportSource::Stdin => "stdin".to_string(),
                };

                return Err(Box::new(io::Error::other(format!(
                    "In {}, the first block is {}-{}, and its parent (hash: {}) was not found in the database. The current tip is {}-{}.",
                    source_display,
                    first_block.number(),
                    first_block.hash(),
                    first_block_parent,
                    tip.number(),
                    tip.hash(),
                ))));
            }
        }
```

**File:** util/instrument/src/import.rs (L188-192)
```rust
        for (block, block_size) in blocks_rx {
            if !block.is_genesis() {
                use ckb_chain::LonelyBlock;

                largest_block_number = largest_block_number.max(block.number());
```

**File:** util/instrument/src/import.rs (L205-213)
```rust
                #[cfg(not(feature = "progress_bar"))]
                let callback = {
                    let _ = block_size;
                    Box::new(move |verify_result: VerifyResult| {
                        if let Err(err) = verify_result {
                            eprintln!("Error verifying block: {:?}", err);
                        }
                    })
                };
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

**File:** chain/src/chain_service.rs (L121-130)
```rust
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

**File:** verification/src/block_verifier.rs (L146-152)
```rust
        let cellbase_input = &cellbase_transaction
            .inputs()
            .get(0)
            .expect("cellbase should have input");
        if cellbase_input != &CellInput::new_cellbase_input(block.header().number()) {
            return Err((CellbaseError::InvalidInput).into());
        }
```
