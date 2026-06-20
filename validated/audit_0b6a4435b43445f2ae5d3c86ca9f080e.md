### Title
Remote Peer-Triggered Node Panic via `GetBlocksProof` with Frozen Block Hash — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

---

### Summary

When the CKB chain freezer is enabled, `wipe_out_frozen_data` deletes `COLUMN_BLOCK_UNCLE` for frozen blocks but leaves their entries in `COLUMN_INDEX` intact. Because `is_main_chain` only checks `COLUMN_INDEX`, it returns `true` for frozen block hashes. The `GetBlocksProofProcess::execute` handler then calls `get_block_uncles(...).expect("block uncles must be stored")` on a frozen block, which returns `None` and panics, crashing the node process.

---

### Finding Description

**`is_main_chain` checks only `COLUMN_INDEX`:** [1](#0-0) 

**The freezer's `wipe_out_frozen_data` deletes `COLUMN_BLOCK_UNCLE` via `delete_block_body`, but does NOT delete `COLUMN_INDEX` entries:** [2](#0-1) [3](#0-2) 

**`get_block_uncles` has no freezer fallback — it reads only from `COLUMN_BLOCK_UNCLE`:** [4](#0-3) 

**The handler calls `.expect()` unconditionally after `is_main_chain` returns `true`:** [5](#0-4) 

By contrast, `get_block` (used for `last_block` at line 51–53) has an explicit freezer path that returns early from the freezer file before ever calling `get_block_uncles`: [6](#0-5) 

`get_block_uncles` called directly at line 88–90 has no such guard.

---

### Impact Explanation

An unprivileged remote peer that can send a `GetBlocksProof` light-client protocol message can crash the full node process. The panic is an unwrap on `None` in an async handler, which aborts the process. Impact: **node crash / denial of service**.

---

### Likelihood Explanation

The freezer is a supported production feature (opt-in, disabled by default per the CHANGELOG). Operators running archive or long-running nodes may enable it. All frozen block hashes are public information on the blockchain, so the attacker needs no privileged knowledge — only a valid recent `last_hash` (any tip-adjacent block) and any frozen block hash in `block_hashes`. The attack is trivially constructable.

---

### Recommendation

In `execute()`, before calling `get_block_uncles(...).expect(...)`, check whether the block is frozen and retrieve uncle data from the freezer if so — mirroring the pattern already used in `get_block`. Alternatively, replace the `.expect()` with graceful error handling (return an error `Status` or skip the block) so a missing uncle entry never causes a panic reachable from a remote message.

---

### Proof of Concept

1. Start a CKB node with the freezer enabled and let it run until at least one epoch boundary triggers `freeze()` → `wipe_out_frozen_data()`, deleting `COLUMN_BLOCK_UNCLE` for frozen blocks while leaving `COLUMN_INDEX` intact.
2. Obtain any frozen block hash `H` (e.g., block #1) and any current main-chain tip hash `T`.
3. Connect as a light-client peer and send:
   ```
   GetBlocksProof {
       last_hash: T,       // passes is_main_chain check
       block_hashes: [H],  // frozen: is_main_chain=true, get_block_uncles=None
   }
   ```
4. The node executes `get_block_uncles(H).expect("block uncles must be stored")`, receives `None`, and panics — crashing the process. [7](#0-6)

### Citations

**File:** store/src/store.rs (L42-58)
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
```

**File:** store/src/store.rs (L205-224)
```rust
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

**File:** store/src/write_batch.rs (L91-98)
```rust
    pub fn delete_block_body(
        &mut self,
        number: BlockNumber,
        hash: &packed::Byte32,
        txs_len: u32,
    ) -> Result<(), Error> {
        self.inner.delete(COLUMN_BLOCK_UNCLE, hash.as_slice())?;
        self.inner.delete(COLUMN_BLOCK_EXTENSION, hash.as_slice())?;
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

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L72-90)
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
```
