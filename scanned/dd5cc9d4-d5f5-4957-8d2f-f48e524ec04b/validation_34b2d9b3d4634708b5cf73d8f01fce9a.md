The code evidence is now complete. Let me trace the full chain precisely.

**Key facts established:**

1. `delete_block_body` in `store/src/write_batch.rs` deletes `COLUMN_BLOCK_UNCLE` (line 97) but does NOT delete `COLUMN_BLOCK_HEADER` or `COLUMN_INDEX`.
2. The freezer's `wipe_out_frozen_data` calls `batch.delete_block_body()` for every frozen block (comment: "remain header").
3. `is_main_chain()` reads from `COLUMN_INDEX` — kept after freezing.
4. `get_block_header()` reads from `COLUMN_BLOCK_HEADER` — kept after freezing.
5. `get_block_uncles()` reads from `COLUMN_BLOCK_UNCLE` — **deleted** after freezing.
6. `get_block()` is freezer-aware (lines 44–54 of `store/src/store.rs`) and retrieves from flat files for frozen blocks. `get_block_uncles()` called directly in `get_blocks_proof.rs` is **not** freezer-aware.

---

### Title
Remote peer triggers node panic via `get_block_uncles().expect()` on freezer-pruned uncle data in `GetBlocksProofProcess` — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

### Summary

When the CKB freezer is enabled, ancient block uncle data is deleted from `COLUMN_BLOCK_UNCLE` while the main-chain index (`COLUMN_INDEX`) and block header (`COLUMN_BLOCK_HEADER`) are retained. `GetBlocksProofProcess::execute` calls `get_block_uncles().expect("block uncles must be stored")` directly — without freezer-aware fallback — after confirming `is_main_chain()`. A remote peer sending a `GetBlocksProof` message containing any frozen block hash causes an unconditional panic.

### Finding Description

In `get_blocks_proof.rs` lines 81–94, for each hash that passes `is_main_chain()`, the code calls:

```rust
let header = snapshot
    .get_block_header(&block_hash)
    .expect("header should be in store");   // line 83-84 — safe: header kept

let uncles = snapshot
    .get_block_uncles(&block_hash)
    .expect("block uncles must be stored"); // line 89-90 — PANICS for frozen blocks
```

`get_block_uncles()` reads only from `COLUMN_BLOCK_UNCLE`. [1](#0-0) 

`delete_block_body()` — called by the freezer's `wipe_out_frozen_data` — explicitly deletes `COLUMN_BLOCK_UNCLE` (line 97) while leaving `COLUMN_BLOCK_HEADER` and `COLUMN_INDEX` intact (the "remain header" design). [2](#0-1) 

The freezer background thread runs `wipe_out_frozen_data` after moving ancient blocks to flat files: [3](#0-2) 

By contrast, `get_block()` in `ChainStore` has an explicit freezer-aware branch that retrieves the full block (including uncles) from flat files for frozen blocks, bypassing `get_block_uncles()`. `get_blocks_proof.rs` calls `get_block_uncles()` directly, skipping this path entirely. [4](#0-3) 

The `is_main_chain()` check reads `COLUMN_INDEX`, which is never deleted by the freezer, so it returns `true` for frozen blocks. [5](#0-4) 

The note in the question about the **header** being missing is incorrect — headers are preserved. The actual panic site is the **uncle** `.expect()` at line 89–90. [6](#0-5) 

### Impact Explanation

Any remote peer connected to a light-client-protocol-enabled node with the freezer active can crash the node by sending a single `GetBlocksProof` message containing any frozen block hash. The hash is trivially discoverable (it is a canonical main-chain block). The panic unwinds the async task and crashes the process. [7](#0-6) 

### Likelihood Explanation

The freezer is disabled by default but is a documented, supported production feature introduced in v0.40.0. Operators running archival or storage-optimized nodes are the target audience. Once enabled, every frozen block hash (all blocks older than ~2 epochs before the threshold) is a valid trigger. The attacker needs no credentials, no PoW, and no special knowledge beyond knowing any old block hash (publicly available from any block explorer).

### Recommendation

Replace the direct `get_block_uncles().expect()` call with a freezer-aware path, mirroring the logic in `get_block()`: if the block number falls below `freezer.number()`, retrieve the full block from the freezer flat file and extract uncles from it. Alternatively, return an error `Status` instead of panicking when uncle data is absent for a main-chain block, consistent with how `reply_proof` handles MMR errors. [8](#0-7) 

### Proof of Concept

1. Enable the freezer in node config; let the chain advance past the freeze threshold so at least one block is frozen (uncle data deleted from `COLUMN_BLOCK_UNCLE`, index and header retained).
2. Connect a peer to the light-client protocol port.
3. Send a `GetBlocksProof` message where `block_hashes` contains any frozen block hash and `last_hash` is the current tip (passes `is_main_chain()`).
4. `is_main_chain(frozen_hash)` → `true`; `get_block_header(frozen_hash)` → `Some`; `get_block_uncles(frozen_hash)` → `None` → `.expect()` panics → node crashes. [9](#0-8)

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

**File:** shared/src/shared.rs (L220-226)
```rust
        if !frozen.is_empty() {
            // remain header
            for (hash, (number, txs)) in &frozen {
                batch.delete_block_body(*number, hash, *txs).map_err(|e| {
                    ckb_logger::error!("Freezer delete_block_body failed {}", e);
                    e
                })?;
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

**File:** store/src/store.rs (L278-281)
```rust
    /// Returns true if the block is on the main chain.
    fn is_main_chain(&self, hash: &packed::Byte32) -> bool {
        self.get(COLUMN_INDEX, hash.as_slice()).is_some()
    }
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L72-95)
```rust
        let (found, missing): (Vec<_>, Vec<_>) = block_hashes
            .into_iter()
            .partition(|block_hash| snapshot.is_main_chain(block_hash));

        let mut positions = Vec::with_capacity(found.len());
        let mut block_headers = Vec::with_capacity(found.len());
        let mut uncles_hash = Vec::with_capacity(found.len());
        let mut extensions = Vec::with_capacity(found.len());

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

**File:** util/light-client-protocol-server/src/lib.rs (L113-117)
```rust
            packed::LightClientMessageUnionReader::GetBlocksProof(reader) => {
                components::GetBlocksProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
```

**File:** util/light-client-protocol-server/src/lib.rs (L200-214)
```rust
            let parent_chain_root = match mmr.get_root() {
                Ok(root) => root,
                Err(err) => {
                    let errmsg = format!("failed to generate a root since {err:?}");
                    return StatusCode::InternalError.with_context(errmsg);
                }
            };
            let proof = if items_positions.is_empty() {
                Default::default()
            } else {
                match mmr.gen_proof(items_positions) {
                    Ok(proof) => proof.proof_items().to_owned(),
                    Err(err) => {
                        let errmsg = format!("failed to generate a proof since {err:?}");
                        return StatusCode::InternalError.with_context(errmsg);
```
