### Title
Non-Atomic Two-Step Block Insertion Creates Intermediate State That Blocks `find_blocks_to_fetch` on Startup — (`chain/src/init_load_unverified.rs`, `sync/src/synchronizer/mod.rs`)

---

### Summary

CKB's block processing pipeline commits block data and its verification result in **two separate database transactions**. Between these two commits, a block exists in an intermediate "stored-but-unverified" state. On node restart, `InitLoadUnverified` must scan and re-queue every block in this intermediate state before clearing the `is_verifying_unverified_blocks_on_startup` flag. While that flag is `true`, `find_blocks_to_fetch` in the synchronizer is **completely skipped**, halting all active block downloads from peers. An unprivileged block-relay peer can deliberately populate the database with many such intermediate-state blocks, causing the startup scan to run for an extended period and stalling synchronization.

---

### Finding Description

**Two-step, non-atomic block insertion**

When a block arrives from a peer, `ChainService::asynchronous_process_block` first runs `non_contextual_verify` (PoW + structure checks), then commits the block in **Transaction 1**:

```
chain/src/chain_service.rs  lines 146-151
fn insert_block(&self, lonely_block: &LonelyBlock) -> Result<(), ckb_error::Error> {
    let db_txn = self.shared.store().begin_transaction();
    db_txn.insert_block(lonely_block.block())?;   // writes COLUMN_NUMBER_HASH + body
    db_txn.commit()?;
    Ok(())
}
```

`insert_block` writes `COLUMN_NUMBER_HASH` (and all block body columns) but does **not** write `COLUMN_BLOCK_EXT` (`BlockExt`). [1](#0-0) [2](#0-1) 

Full contextual verification happens asynchronously in the `ConsumeUnverifiedBlocks` thread. Only after that succeeds does **Transaction 2** commit `BlockExt` via `insert_block_ext` / `insert_ok_ext`:

```
chain/src/verify.rs  line 357
db_txn.insert_block_ext(&block.header().hash(), &ext)?;
``` [3](#0-2) 

Between Transaction 1 and Transaction 2 the block is in an intermediate state: present in `COLUMN_NUMBER_HASH`, absent from `COLUMN_BLOCK_EXT`.

---

**Intermediate-state detection on startup**

`InitLoadUnverified::find_unverified_block_hashes` explicitly identifies this intermediate state:

```
chain/src/init_load_unverified.rs  lines 40-58
// If a block has `COLUMN_NUMBER_HASH` but not `BlockExt`,
// it indicates an unverified block inserted during the last shutdown.
.filter(|hash| self.shared.store().get_block_ext(hash).is_none())
``` [4](#0-3) 

`InitLoadUnverified::start()` re-queues every such block and only clears the flag **after the entire scan completes**:

```
chain/src/init_load_unverified.rs  lines 61-73
pub(crate) fn start(&self) {
    self.find_and_verify_unverified_blocks();          // blocks until scan done
    self.is_verifying_unverified_blocks_on_startup
        .store(false, Ordering::Release);              // flag cleared only here
}
``` [5](#0-4) 

The flag starts as `true` at node launch:

```
chain/src/init.rs  line 95
let is_verifying_unverified_blocks_on_startup = Arc::new(AtomicBool::new(true));
``` [6](#0-5) 

---

**Critical function blocked by the intermediate state**

`Synchronizer::find_blocks_to_fetch` — the sole mechanism by which the node actively requests blocks from peers — guards itself with this flag:

```
sync/src/synchronizer/mod.rs  lines 735-741
fn find_blocks_to_fetch(&mut self, nc: &Arc<dyn CKBProtocolContext + Sync>, ibd: IBDState) {
    if self.chain.is_verifying_unverified_blocks_on_startup() {
        trace!("skip find_blocks_to_fetch, ckb_chain is verifying unverified blocks on startup");
        return;                    // ← entire function skipped
    }
    ...
}
``` [7](#0-6) 

While the flag is `true`, the node issues **zero** `GetBlocks` requests to any peer. IBD and normal sync both stall.

---

**Runtime analog (no restart required)**

A second blocking path exists at runtime. After Transaction 1, `OrphanBroker::send_unverified_block` advances `unverified_tip`:

```
chain/src/orphan_broker.rs  lines 180-185
if block_number > self.shared.snapshot().tip_number() {
    self.shared.set_unverified_tip(HeaderIndex::new(block_number, ...));
}
``` [8](#0-7) 

`BlockFetcher::fetch` then refuses to request more blocks when the gap is too large:

```
sync/src/synchronizer/block_fetcher.rs  lines 111-129
let unverified_tip_limit = tip_number + BLOCK_DOWNLOAD_WINDOW * 9;
if unverified_tip >= unverified_tip_limit {
    return None;   // ← fetch blocked
}
``` [9](#0-8) 

An attacker who sends enough valid blocks to push `unverified_tip` ≥ `tip + BLOCK_DOWNLOAD_WINDOW × 9` triggers this path without any restart.

---

### Impact Explanation

During the startup scan (or when the runtime `unverified_tip` limit is hit), the node cannot actively download any new blocks. In IBD this means the node cannot catch up to the chain tip. In steady-state operation it means the node falls behind the network, cannot relay compact blocks, and cannot build valid block templates for miners. The duration of the blockage scales linearly with the number of intermediate-state blocks the attacker has pre-loaded.

---

### Likelihood Explanation

**Startup path**: Requires the attacker to have previously relayed many blocks that passed `non_contextual_verify` (including PoW) and for the node to restart before contextual verification finishes. Node restarts are routine (upgrades, crashes, maintenance). A miner with modest hashpower can accumulate many side-chain blocks over time and relay them just before a known maintenance window.

**Runtime path**: Requires sending `BLOCK_DOWNLOAD_WINDOW × 9` (≈ 18 000) valid blocks ahead of the current tip. This is expensive but achievable for a miner or a coalition of miners.

Overall likelihood: **Medium** for the startup path; **Low-Medium** for the runtime path.

---

### Recommendation

1. **Make the two-step insertion atomic**: Combine `insert_block` and the initial unverified-state marker into a single RocksDB transaction that also writes a sentinel `BlockExt{verified: None}`. This eliminates the intermediate state entirely; `InitLoadUnverified` would then scan for `BlockExt{verified: None}` entries, which are always present from the moment a block is stored.

2. **Narrow the scope of `is_verifying_unverified_blocks_on_startup`**: Instead of blocking all of `find_blocks_to_fetch`, only block fetching for block numbers that are already queued for re-verification. Blocks at heights not covered by the startup scan can be fetched normally.

3. **Cap the number of intermediate-state blocks accepted per peer**: Rate-limit or bound how many unverified blocks a single peer can contribute to the `COLUMN_NUMBER_HASH` set, reducing the attacker's ability to inflate the startup scan.

---

### Proof of Concept

```
1. Attacker (a peer with valid PoW capability) relays N valid blocks
   (passing non_contextual_verify) to the target node.

2. chain_service.rs:insert_block() commits each block to COLUMN_NUMBER_HASH
   (Transaction 1). Contextual verification is queued asynchronously.

3. Before all N contextual verifications complete, the node restarts
   (upgrade, crash, or operator action).

4. On restart, init.rs sets is_verifying_unverified_blocks_on_startup = true.

5. InitLoadUnverified::find_unverified_block_hashes() finds all N blocks
   (COLUMN_NUMBER_HASH present, BlockExt absent) and re-queues them via
   asynchronous_process_lonely_block() into the bounded channel (size 24).
   The scan loop blocks on each send when the channel is full, so the
   scan takes O(N) time.

6. During the entire scan, sync/src/synchronizer/mod.rs:find_blocks_to_fetch()
   returns immediately at line 736-740 without issuing any GetBlocks requests.

7. The node cannot download any new blocks from peers until the scan finishes.
   With N = 10 000 intermediate-state blocks, the node may be unable to sync
   for minutes to hours depending on verification throughput.
```

### Citations

**File:** chain/src/chain_service.rs (L146-151)
```rust
    fn insert_block(&self, lonely_block: &LonelyBlock) -> Result<(), ckb_error::Error> {
        let db_txn = self.shared.store().begin_transaction();
        db_txn.insert_block(lonely_block.block())?;
        db_txn.commit()?;
        Ok(())
    }
```

**File:** store/src/transaction.rs (L172-209)
```rust
    pub fn insert_block(&self, block: &BlockView) -> Result<(), Error> {
        let hash = block.hash();
        let header = Into::<packed::HeaderView>::into(block.header());
        let uncles = Into::<packed::UncleBlockVecView>::into(block.uncles());
        let proposals = block.data().proposals();
        let txs_len: packed::Uint32 = (block.transactions().len() as u32).into();
        self.insert_raw(COLUMN_BLOCK_HEADER, hash.as_slice(), header.as_slice())?;
        self.insert_raw(COLUMN_BLOCK_UNCLE, hash.as_slice(), uncles.as_slice())?;
        if let Some(extension) = block.extension() {
            self.insert_raw(
                COLUMN_BLOCK_EXTENSION,
                hash.as_slice(),
                extension.as_slice(),
            )?;
        }
        self.insert_raw(
            COLUMN_NUMBER_HASH,
            packed::NumberHash::new_builder()
                .number(block.number())
                .block_hash(hash.clone())
                .build()
                .as_slice(),
            txs_len.as_slice(),
        )?;
        self.insert_raw(
            COLUMN_BLOCK_PROPOSAL_IDS,
            hash.as_slice(),
            proposals.as_slice(),
        )?;
        for (index, tx) in block.transactions().into_iter().enumerate() {
            let key = packed::TransactionKey::new_builder()
                .block_hash(hash.clone())
                .index(index)
                .build();
            let tx_data = Into::<packed::TransactionView>::into(tx);
            self.insert_raw(COLUMN_BLOCK_BODY, key.as_slice(), tx_data.as_slice())?;
        }
        Ok(())
```

**File:** chain/src/verify.rs (L356-359)
```rust
        } else {
            db_txn.insert_block_ext(&block.header().hash(), &ext)?;
        }
        db_txn.commit()?;
```

**File:** chain/src/init_load_unverified.rs (L40-58)
```rust
        // If a block has `COLUMN_NUMBER_HASH` but not `BlockExt`,
        // it indicates an unverified block inserted during the last shutdown.
        let unverified_hashes: Vec<packed::Byte32> = self
            .shared
            .store()
            .get_iter(
                COLUMN_NUMBER_HASH,
                IteratorMode::From(prefix, Direction::Forward),
            )
            .take_while(|(key, _)| key.starts_with(prefix))
            .map(|(key_number_hash, _v)| {
                let reader =
                    packed::NumberHashReader::from_slice_should_be_ok(key_number_hash.as_ref());

                reader.block_hash().to_entity()
            })
            .filter(|hash| self.shared.store().get_block_ext(hash).is_none())
            .collect::<Vec<packed::Byte32>>();
        unverified_hashes
```

**File:** chain/src/init_load_unverified.rs (L61-73)
```rust
    pub(crate) fn start(&self) {
        info!(
            "finding unverified blocks, current tip: {}-{}",
            self.shared.snapshot().tip_number(),
            self.shared.snapshot().tip_hash()
        );

        self.find_and_verify_unverified_blocks();

        self.is_verifying_unverified_blocks_on_startup
            .store(false, std::sync::atomic::Ordering::Release);
        info!("find unverified blocks finished");
    }
```

**File:** chain/src/init.rs (L95-95)
```rust
    let is_verifying_unverified_blocks_on_startup = Arc::new(AtomicBool::new(true));
```

**File:** sync/src/synchronizer/mod.rs (L735-741)
```rust
    fn find_blocks_to_fetch(&mut self, nc: &Arc<dyn CKBProtocolContext + Sync>, ibd: IBDState) {
        if self.chain.is_verifying_unverified_blocks_on_startup() {
            trace!(
                "skip find_blocks_to_fetch, ckb_chain is verifying unverified blocks on startup"
            );
            return;
        }
```

**File:** chain/src/orphan_broker.rs (L180-185)
```rust
        if block_number > self.shared.snapshot().tip_number() {
            self.shared.set_unverified_tip(ckb_shared::HeaderIndex::new(
                block_number,
                block_hash.clone(),
                U256::from(0u64),
            ));
```

**File:** sync/src/synchronizer/block_fetcher.rs (L111-129)
```rust
        let Some(unverified_tip_limit) = self
            .sync_shared
            .active_chain()
            .tip_number()
            .checked_add(BLOCK_DOWNLOAD_WINDOW * 9)
        else {
            trace!(
                "active chain tip is too close to BlockNumber::MAX to calculate unverified tip limit"
            );
            return None;
        };
        if self.sync_shared.shared().get_unverified_tip().number() >= unverified_tip_limit {
            trace!(
                "unverified_tip - tip > BLOCK_DOWNLOAD_WINDOW * 9, skip fetch, unverified_tip: {}, tip: {}",
                self.sync_shared.shared().get_unverified_tip().number(),
                self.sync_shared.active_chain().tip_number()
            );
            return None;
        }
```
