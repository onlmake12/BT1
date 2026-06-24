Audit Report

## Title
Unprotected `expect()` Panics in `PreloadUnverifiedBlocksChannel` Permanently Halt Block Verification Pipeline — (`chain/src/preload_unverified_blocks_channel.rs`)

## Summary

`PreloadUnverifiedBlocksChannel::load_full_unverified_block_by_hash` contains two bare `.expect()` calls with no `catch_unwind` protection. When `ConsumeUnverifiedBlocks` deletes a failed block N from the store (removing its header from `COLUMN_BLOCK_HEADER`), the preload thread processing N's descendant N+1 calls `get_block_header(&hash(N))`, receives `None`, and panics. The thread dies permanently, dropping `unverified_block_tx`, which causes `ConsumeUnverifiedBlocks` to exit on the resulting channel error. The entire block verification pipeline is irreversibly halted.

## Finding Description

**Root cause — bare `.expect()` with no panic recovery:**

`load_full_unverified_block_by_hash` in `chain/src/preload_unverified_blocks_channel.rs` (lines 85–96) issues two store lookups that can return `None`:

```rust
let block_view = self.shared.store()
    .get_block(&block_number_and_hash.hash())
    .expect("block stored");          // panics if block deleted

let parent_header = self.shared.store()
    .get_block_header(&parent_hash)
    .expect("parent header stored");  // panics if parent deleted
``` [1](#0-0) 

The `start()` loop that calls this function has no `catch_unwind`: [2](#0-1) 

**`delete_block` removes the header from the store:**

`StoreTransaction::delete_block` explicitly deletes `COLUMN_BLOCK_HEADER`:

```rust
pub fn delete_block(&self, block: &BlockView) -> Result<(), Error> {
    let hash = block.hash();
    self.delete(COLUMN_BLOCK_HEADER, hash.as_slice())?;  // header gone
    ...
}
``` [3](#0-2) 

`delete_unverified_block` in `chain/src/lib.rs` calls this: [4](#0-3) 

**Race condition — preload thread runs ahead of consume thread:**

The `unverified_block_tx` channel has capacity 128, so the preload thread can be up to 128 blocks ahead of `ConsumeUnverifiedBlocks`. The sequence:

1. Preload thread processes N → sends `UnverifiedBlock(N)` to channel (capacity available).
2. Preload thread immediately processes N+1 → calls `get_block_header(&hash(N))`.
3. Concurrently, `ConsumeUnverifiedBlocks` processes N → contextual verification fails → calls `delete_unverified_block(N)` → `COLUMN_BLOCK_HEADER` entry for N is deleted.
4. Preload thread's `get_block_header` returns `None` → `.expect("parent header stored")` **panics**.

Channel sizes confirmed: [5](#0-4) 

**Cascade — `ConsumeUnverifiedBlocks` exits on channel error:**

When the preload thread panics, `unverified_block_tx` (the sender) is dropped. `ConsumeUnverifiedBlocks::start()` receives `Err` on `unverified_block_rx` and returns permanently: [6](#0-5) 

**Contrast with `ConsumeUnverifiedBlocks` which does use `catch_unwind`:** [7](#0-6) 

The preload thread has no equivalent protection.

## Impact Explanation

Once the preload thread panics: the `unverified_block_tx` sender is dropped; `ConsumeUnverifiedBlocks` exits permanently; no further blocks can be contextually verified or committed; the node's chain tip freezes with no automatic recovery path. The node process remains alive but is functionally dead for block processing — it cannot advance the chain, participate in consensus, or relay new blocks. Manual process restart is the only remedy. This matches **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

The attacker must produce two blocks with valid PoW: block N (valid PoW, invalid contextual content such as a script that always fails) and block N+1 (valid PoW, valid content, child of N). Both must pass `ChainService` non-contextual verification and enter the preload queue before N is deleted. The `preload_unverified_rx` channel holds up to `BLOCK_DOWNLOAD_WINDOW * 10 ≈ 81,920` items, and `unverified_block_tx` holds 128, giving a wide race window. The attack requires miner-level hashrate, making it expensive but entirely feasible for any mining pool. It is a one-time cost for a permanent effect, with no self-healing mechanism.

## Recommendation

1. **Wrap the preload thread's loop body in `catch_unwind`**, mirroring the pattern in `ConsumeUnverifiedBlocks`. On panic, log the error and continue the loop rather than dying.
2. **Replace `.expect()` with graceful error handling** in `load_full_unverified_block_by_hash`. If `get_block` or `get_block_header` returns `None`, log a warning, invoke the verify callback with an error result, and skip the item.
3. **Propagate `BLOCK_INVALID` status to queued descendants**: when `ConsumeUnverifiedBlocks` marks block N as invalid, the preload thread should check the parent's block status before attempting a store lookup and skip items whose parent is already marked invalid.

## Proof of Concept

```
1. Attacker (miner) mines block N at current chain tip:
   - Valid PoW, but contains a transaction with an always-failing lock script.
   - N passes ChainService non-contextual verification → stored → enters preload queue.

2. Attacker immediately mines block N+1 (child of N):
   - Valid PoW, valid transactions.
   - N+1 passes ChainService non-contextual verification → stored → enters preload queue.

3. PreloadUnverifiedBlocksChannel processes N:
   - get_block(&hash(N)) → Some(...) ✓
   - get_block_header(&hash(N-1)) → Some(...) ✓
   - Sends UnverifiedBlock(N) to unverified_block_tx (channel has capacity).

4. PreloadUnverifiedBlocksChannel immediately processes N+1:
   - get_block(&hash(N+1)) → Some(...) ✓
   - get_block_header(&hash(N)) → [concurrent with step 5 below]

5. ConsumeUnverifiedBlocks processes UnverifiedBlock(N):
   - Contextual verification fails (invalid script).
   - Calls delete_unverified_block(N) → StoreTransaction::delete_block removes
     COLUMN_BLOCK_HEADER entry for hash(N).

6. Preload thread's get_block_header(&hash(N)) returns None.
   - .expect("parent header stored") → PANIC.
   - preload_unverified_block thread dies.
   - unverified_block_tx sender dropped.

7. ConsumeUnverifiedBlocks receives Err on unverified_block_rx → returns permanently.

8. Block verification pipeline is permanently halted. Node chain tip frozen.
   Only recovery: manual process restart.
```

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

**File:** chain/src/preload_unverified_blocks_channel.rs (L85-96)
```rust
        let block_view = self
            .shared
            .store()
            .get_block(&block_number_and_hash.hash())
            .expect("block stored");
        let block = Arc::new(block_view);
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

**File:** chain/src/lib.rs (L189-231)
```rust
pub(crate) fn delete_unverified_block(
    store: &ChainDB,
    block_hash: Byte32,
    block_number: BlockNumber,
    parent_hash: Byte32,
) {
    info!(
        "parent: {}, deleting this block {}-{}",
        parent_hash, block_number, block_hash,
    );

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
            if let Err(err) = db_txn.commit() {
                error!(
                    "commit delete block {}-{} failed {:?}",
                    block_number, block_hash, err
                );
                return;
            }

            info!(
                "parent: {}, deleted this block {}-{}",
                parent_hash, block_number, block_hash,
            );
        }
        None => {
            error!(
                "want to delete block {}-{}, but it not found in db",
                block_number, block_hash
            );
        }
    }
}
```

**File:** chain/src/init.rs (L49-53)
```rust
    let (preload_unverified_tx, preload_unverified_rx) =
        channel::bounded::<LonelyBlockHash>(BLOCK_DOWNLOAD_WINDOW as usize * 10);

    let (unverified_queue_stop_tx, unverified_queue_stop_rx) = ckb_channel::bounded::<()>(1);
    let (unverified_block_tx, unverified_block_rx) = channel::bounded::<UnverifiedBlock>(128usize);
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
