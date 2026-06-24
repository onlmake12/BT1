All code claims verified against the actual repository. Every cited line matches exactly.

- `delete_block_body` at line 97 deletes `COLUMN_BLOCK_UNCLE` but not `COLUMN_BLOCK_HEADER` or `COLUMN_INDEX`. [1](#0-0) 
- `get_block_uncles` has no freezer fallback — only reads `COLUMN_BLOCK_UNCLE`. [2](#0-1) 
- `is_main_chain` reads `COLUMN_INDEX`, which is never deleted during freezing. [3](#0-2) 
- `get_block` correctly checks the freezer first before falling through. [4](#0-3) 
- `GetBlocksProofProcess::execute` calls `.expect("block uncles must be stored")` unconditionally after `is_main_chain` partitions the hash into `found`. [5](#0-4) 
- `wipe_out_frozen_data` calls `delete_block_body` with the comment "remain header", confirming the asymmetric deletion. [6](#0-5) 

---

Audit Report

## Title
Remote Peer Can Panic Light-Client Node via `GetBlocksProof` on Frozen Block — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

## Summary
When the freezer is enabled, `wipe_out_frozen_data` calls `delete_block_body`, which removes `COLUMN_BLOCK_UNCLE` while intentionally leaving `COLUMN_BLOCK_HEADER` and `COLUMN_INDEX` intact. `GetBlocksProofProcess::execute` partitions requested hashes by `is_main_chain` (which reads `COLUMN_INDEX`, still present), then calls `get_block_uncles` (which has no freezer fallback and returns `None` for frozen blocks), followed by an unconditional `.expect("block uncles must be stored")` that panics and terminates the node process. Any remote peer with a P2P connection can trigger this with a single well-formed `GetBlocksProof` message referencing any frozen block hash.

## Finding Description
`StoreWriteBatch::delete_block_body` deletes `COLUMN_BLOCK_UNCLE` for every block moved to the freezer but leaves `COLUMN_BLOCK_HEADER` and `COLUMN_INDEX` untouched (the comment in `wipe_out_frozen_data` reads "remain header"). As a result, for any frozen block hash `h`:

- `is_main_chain(h)` → `true` (`COLUMN_INDEX` present)
- `get_block_header(h)` → `Some(...)` (`COLUMN_BLOCK_HEADER` present)
- `get_block_uncles(h)` → `None` (`COLUMN_BLOCK_UNCLE` deleted; `get_block_uncles` has no freezer fallback, unlike `get_block` and `get_transaction_with_info` which both check `self.freezer()` first)

In `GetBlocksProofProcess::execute`, the loop at lines 81–95 iterates over all hashes that passed `is_main_chain`. For each, it calls `get_block_uncles` and immediately calls `.expect("block uncles must be stored")`. For a frozen block this returns `None`, causing an unconditional panic. No guard exists between the `is_main_chain` check and the `get_block_uncles` call to detect the frozen state.

## Impact Explanation
The panic terminates the CKB node process. This matches the allowed High impact: **"Vulnerabilities which could easily crash a CKB node."** The crash is immediate, repeatable, and requires no recovery action from the attacker — the node stays down until manually restarted, and the attack can be repeated immediately after restart.

## Likelihood Explanation
- The freezer is a supported production feature; nodes that enable it and serve the light-client protocol are directly vulnerable.
- The attacker requires only a P2P connection and any frozen block hash. All main-chain block hashes are public and trivially obtained from any block explorer or by syncing headers.
- The trigger is a single well-formed `GetBlocksProof` message — no proof-of-work, no key material, no majority hashpower, and no victim interaction required.
- The attack is repeatable indefinitely.

## Recommendation
In `GetBlocksProofProcess::execute`, replace the direct `get_block_uncles` call with the freezer-aware `get_block` path (already implemented in `ChainStore::get_block`), extracting the uncles hash from the returned `BlockView`. Alternatively, add a freezer fallback inside `get_block_uncles` mirroring the pattern in `get_block` and `get_transaction_with_info`. At minimum, replace the `.expect` with a graceful error return so a missing uncle causes the block to be treated as not found rather than crashing the process.

## Proof of Concept
1. Start a CKB node with the freezer enabled and the light-client protocol server active.
2. Wait for the freezer to advance past block N (i.e., `freezer.number() > N`), causing `wipe_out_frozen_data` to call `delete_block_body` for block N, deleting its `COLUMN_BLOCK_UNCLE` entry.
3. From any peer, send a `GetBlocksProof` message with `block_hashes: [hash_of_block_N]` and `last_hash: <any current tip hash>`.
4. The handler calls `is_main_chain(hash_of_block_N)` → `true`, enters the `found` loop, calls `get_block_uncles(hash_of_block_N)` → `None`, hits `.expect("block uncles must be stored")`, and the node process panics and terminates.

### Citations

**File:** store/src/write_batch.rs (L91-119)
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
    }
```

**File:** store/src/store.rs (L42-54)
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

**File:** store/src/store.rs (L279-281)
```rust
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
