The code evidence is conclusive. Let me trace the exact exploit path.

**Key facts established:**

1. `delete_block_body` in `store/src/write_batch.rs` line 97 explicitly deletes `COLUMN_BLOCK_UNCLE` when blocks are frozen, but does **not** delete `COLUMN_BLOCK_HEADER` or `COLUMN_INDEX`.

2. `get_block_uncles` in `store/src/store.rs` lines 205-224 reads **only** from `COLUMN_BLOCK_UNCLE` — it never checks the freezer.

3. `is_main_chain` in `store/src/store.rs` line 279-281 reads from `COLUMN_INDEX`, which is **not** deleted during freezing.

4. `get_blocks_proof.rs` line 90 calls `.expect("block uncles must be stored")` on the result of `get_block_uncles` — no freezer-awareness.

---

### Title
Remote Peer Can Panic Light-Client Node via `GetBlocksProof` on Frozen Block — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

### Summary
When the freezer is enabled, `delete_block_body` removes uncle data from `COLUMN_BLOCK_UNCLE` while leaving the header in `COLUMN_BLOCK_HEADER` and the index entry in `COLUMN_INDEX`. Any remote peer can send a `GetBlocksProof` message referencing a frozen block hash. The handler calls `is_main_chain` (returns `true`), then `get_block_header` (returns `Some`), then `get_block_uncles` (returns `None` — freezer not consulted), and then unconditionally panics via `.expect("block uncles must be stored")`.

### Finding Description

`StoreWriteBatch::delete_block_body` is called by `wipe_out_frozen_data` for every block moved to the freezer:

```rust
// store/src/write_batch.rs:97
self.inner.delete(COLUMN_BLOCK_UNCLE, hash.as_slice())?;
``` [1](#0-0) 

The header is intentionally kept (comment: "remain header"), and `COLUMN_INDEX` is not touched, so `is_main_chain` continues to return `true` for frozen blocks:

```rust
// store/src/store.rs:279-281
fn is_main_chain(&self, hash: &packed::Byte32) -> bool {
    self.get(COLUMN_INDEX, hash.as_slice()).is_some()
}
``` [2](#0-1) 

`get_block_uncles` has no freezer fallback — it only reads `COLUMN_BLOCK_UNCLE`:

```rust
// store/src/store.rs:212
let ret = self.get(COLUMN_BLOCK_UNCLE, hash.as_slice()).map(|slice| { ... });
``` [3](#0-2) 

Contrast with `get_block`, which correctly checks the freezer first before falling through to `COLUMN_BLOCK_UNCLE`: [4](#0-3) 

In `GetBlocksProofProcess::execute`, the loop at lines 81–95 partitions hashes by `is_main_chain`, then for each "found" hash calls `get_block_uncles` with an unconditional `.expect`:

```rust
// get_blocks_proof.rs:88-90
let uncles = snapshot
    .get_block_uncles(&block_hash)
    .expect("block uncles must be stored");  // panics for frozen blocks
``` [5](#0-4) 

### Impact Explanation

The panic terminates the CKB node process. Any remote peer that knows a frozen block hash (all main-chain hashes are public) can crash a node running both the freezer and the light-client protocol server. The secondary claim about mismatched vector lengths is moot — the panic at line 90 fires before any vector push at line 93, so the process dies before `proved_items` is ever assembled. [6](#0-5) 

### Likelihood Explanation

- The freezer is a supported production feature (opt-in). Nodes that enable it and also serve the light-client protocol are directly vulnerable.
- The attacker needs zero privileges: just a P2P connection and any frozen block hash (trivially obtained from any block explorer or by syncing headers).
- The trigger is a single well-formed `GetBlocksProof` message — no PoW, no key, no majority hashpower required.

### Recommendation

In `GetBlocksProofProcess::execute`, replace the direct `get_block_uncles` call with the freezer-aware `get_block` path (as used in `ChainStore::get_block`), or add a freezer fallback inside `get_block_uncles` mirroring the pattern already present in `get_block`. At minimum, replace the `.expect` with a graceful error return so a missing uncle causes the block to be treated as missing rather than crashing the node.

### Proof of Concept

```rust
// Inject a store where:
//   is_main_chain(frozen_hash) == true   (COLUMN_INDEX entry present)
//   get_block_header(frozen_hash) == Some (COLUMN_BLOCK_HEADER entry present)
//   get_block_uncles(frozen_hash) == None (COLUMN_BLOCK_UNCLE entry deleted by delete_block_body)
//
// Send GetBlocksProof { block_hashes: [frozen_hash], last_hash: tip_hash }
// -> execute() partitions frozen_hash into `found`
// -> get_block_uncles returns None
// -> .expect("block uncles must be stored") panics
// -> node process terminates
```

This is exactly the state produced by the normal freezer lifecycle: `freeze()` writes the block to flat files, then `wipe_out_frozen_data()` calls `delete_block_body()` which deletes `COLUMN_BLOCK_UNCLE` while leaving `COLUMN_BLOCK_HEADER` and `COLUMN_INDEX` intact. [7](#0-6)

### Citations

**File:** store/src/write_batch.rs (L91-118)
```rust
    pub fn delete_block_body(
        &mut self,
        number: BlockNumber,
        hash: &packed::Byte32,
        txs_len: u32,
    ) -> Result<(), Error> {
        self.inner.delete(COLUMN_BLOCK_UNCLE, hash.as_slice())?;
        self.inner.delete(COLUMN_BLOCK_EXTENSION, hash.as_slice())?;
        self.inner
            .delete(COLUMN_BLOCK_PROPOSAL_IDS, hash.as_slice())?;
        self.inner.delete(
            COLUMN_NUMBER_HASH,
            packed::NumberHash::new_builder()
                .number(number)
                .block_hash(hash.clone())
                .build()
                .as_slice(),
        )?;

        let key_range = (0u32..txs_len).map(|i| {
            packed::TransactionKey::new_builder()
                .block_hash(hash.clone())
                .index(i)
                .build()
        });

        self.inner.delete_range(COLUMN_BLOCK_BODY, key_range)?;
        Ok(())
```

**File:** store/src/store.rs (L42-70)
```rust
    fn get_block(&self, h: &packed::Byte32) -> Option<BlockView> {
        let header = self.get_block_header(h)?;
        if let Some(freezer) = self.freezer()
            && header.number() > 0
            && header.number() < freezer.number()
        {
            let raw_block = freezer.retrieve(header.number()).expect("block frozen")?;
            let raw_block_reader =
                packed::BlockReader::from_compatible_slice(&raw_block).expect("checked data");
            if raw_block_reader.calc_header_hash().as_slice() == h.as_slice() {
                return Some(raw_block_reader.to_entity().into_view());
            }
        }
        let body = self.get_block_body(h);
        let uncles = self
            .get_block_uncles(h)
            .expect("block uncles must be stored");
        let proposals = self
            .get_block_proposal_txs_ids(h)
            .expect("block proposal_ids must be stored");
        let extension_opt = self.get_block_extension(h);

        let block = if let Some(extension) = extension_opt {
            BlockView::new_unchecked_with_extension(header, uncles, body, proposals, extension)
        } else {
            BlockView::new_unchecked(header, uncles, body, proposals)
        };
        Some(block)
    }
```

**File:** store/src/store.rs (L204-224)
```rust
    /// Get block uncles by block header hash
    fn get_block_uncles(&self, hash: &packed::Byte32) -> Option<UncleBlockVecView> {
        if let Some(cache) = self.cache()
            && let Some(data) = cache.block_uncles.lock().get(hash)
        {
            return Some(data.clone());
        };

        let ret = self.get(COLUMN_BLOCK_UNCLE, hash.as_slice()).map(|slice| {
            let reader = packed::UncleBlockVecViewReader::from_slice_should_be_ok(slice.as_ref());
            Into::<UncleBlockVecView>::into(reader)
        });

        if let Some(cache) = self.cache() {
            ret.inspect(|uncles| {
                cache.block_uncles.lock().put(hash.clone(), uncles.clone());
            })
        } else {
            ret
        }
    }
```

**File:** store/src/store.rs (L278-281)
```rust
    /// Returns true if the block is on the main chain.
    fn is_main_chain(&self, hash: &packed::Byte32) -> bool {
        self.get(COLUMN_INDEX, hash.as_slice()).is_some()
    }
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L81-95)
```rust
        for block_hash in found {
            let header = snapshot
                .get_block_header(&block_hash)
                .expect("header should be in store");
            positions.push(leaf_index_to_pos(header.number()));
            block_headers.push(header.data());

            let uncles = snapshot
                .get_block_uncles(&block_hash)
                .expect("block uncles must be stored");
            let extension = snapshot.get_block_extension(&block_hash);

            uncles_hash.push(uncles.data().calc_uncles_hash());
            extensions.push(packed::BytesOpt::new_builder().set(extension).build());
        }
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L97-101)
```rust
        let proved_items = (
            block_headers.into(),
            uncles_hash.into(),
            packed::BytesOptVec::new_builder().set(extensions).build(),
        );
```

**File:** shared/src/shared.rs (L209-250)
```rust
    fn wipe_out_frozen_data(
        &self,
        snapshot: &Snapshot,
        frozen: BTreeMap<packed::Byte32, (BlockNumber, u32)>,
        stopped: bool,
    ) -> Result<(), Error> {
        let mut side = BTreeMap::new();
        let mut batch = self.store.new_write_batch();

        ckb_logger::trace!("freezer wipe_out_frozen_data {} ", frozen.len());

        if !frozen.is_empty() {
            // remain header
            for (hash, (number, txs)) in &frozen {
                batch.delete_block_body(*number, hash, *txs).map_err(|e| {
                    ckb_logger::error!("Freezer delete_block_body failed {}", e);
                    e
                })?;

                let pack_number: packed::Uint64 = number.into();
                let prefix = pack_number.as_slice();
                for (key, value) in snapshot
                    .get_iter(
                        COLUMN_NUMBER_HASH,
                        IteratorMode::From(prefix, Direction::Forward),
                    )
                    .take_while(|(key, _)| key.starts_with(prefix))
                {
                    let reader = packed::NumberHashReader::from_slice_should_be_ok(key.as_ref());
                    let block_hash = reader.block_hash().to_entity();
                    if &block_hash != hash {
                        let txs =
                            packed::Uint32Reader::from_slice_should_be_ok(value.as_ref()).into();
                        side.insert(block_hash, (reader.number().to_entity(), txs));
                    }
                }
            }
            self.store.write_sync(&batch).map_err(|e| {
                ckb_logger::error!("Freezer write_batch delete failed {}", e);
                e
            })?;
            batch.clear()?;
```
