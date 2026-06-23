### Title
Remote Peer-Triggered Panic via `get_block_uncles` on Frozen Blocks in `GetBlocksProofProcess::execute` — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

---

### Summary

When the CKB freezer is enabled, `GetBlocksProofProcess::execute` panics with `"block uncles must be stored"` when a remote peer requests proof for any block that has been frozen. The freezer deletes `COLUMN_BLOCK_UNCLE` for frozen blocks but leaves `COLUMN_INDEX` intact, so `is_main_chain()` returns `true` while `get_block_uncles()` returns `None`. The unconditional `.expect()` at line 90 then aborts the process.

---

### Finding Description

**Storage invariant broken by the freezer:**

`is_main_chain()` checks `COLUMN_INDEX`: [1](#0-0) 

`get_block_uncles()` reads only from `COLUMN_BLOCK_UNCLE` — **no freezer fallback**: [2](#0-1) 

`wipe_out_frozen_data()` calls `delete_block_body()` for every frozen block, which explicitly deletes `COLUMN_BLOCK_UNCLE` while leaving `COLUMN_INDEX` and `COLUMN_BLOCK_HEADER` intact: [3](#0-2) [4](#0-3) 

Contrast with `get_block()`, which **does** have a freezer fallback path (lines 44–53) before falling through to `get_block_uncles()`: [5](#0-4) 

`get_block_uncles()` has no equivalent fallback.

**The vulnerable code path:**

```
for block_hash in found {                          // found = passed is_main_chain()
    let header = snapshot
        .get_block_header(&block_hash)
        .expect("header should be in store");      // succeeds: COLUMN_BLOCK_HEADER intact
    ...
    let uncles = snapshot
        .get_block_uncles(&block_hash)
        .expect("block uncles must be stored");    // PANICS: COLUMN_BLOCK_UNCLE deleted
``` [6](#0-5) 

---

### Impact Explanation

A Rust `.expect()` failure calls `panic!`, which — in a production binary without a custom panic hook — aborts the process. Any remote peer connected via the light-client P2P protocol can crash the full node by sending a single `GetBlocksProof` message containing any frozen block hash. Frozen block hashes are public information (they appear in the canonical chain). **Impact: full node crash / denial of service.**

---

### Likelihood Explanation

The freezer is a supported production feature (opt-in, disabled by default). Nodes that enable it to save disk space are directly vulnerable. The attacker needs only a TCP connection to the light-client protocol port and knowledge of any block number below the freeze threshold — both trivially obtainable from any block explorer. No PoW, no key, no privileged role required.

---

### Recommendation

Replace the bare `.expect()` with a freezer-aware lookup, mirroring the pattern already used in `get_block()`:

```rust
let uncles = if let Some(freezer) = snapshot.freezer() {
    let header = snapshot.get_block_header(&block_hash)
        .expect("header should be in store");
    if header.number() > 0 && header.number() < freezer.number() {
        // retrieve full block from freezer and extract uncles
        let raw = freezer.retrieve(header.number())
            .expect("block frozen")?;
        let reader = packed::BlockReader::from_compatible_slice(&raw)
            .expect("checked data");
        reader.uncles().to_entity().into_view()
    } else {
        snapshot.get_block_uncles(&block_hash)
            .expect("block uncles must be stored")
    }
} else {
    snapshot.get_block_uncles(&block_hash)
        .expect("block uncles must be stored")
};
```

Alternatively, add a freezer fallback directly inside `ChainStore::get_block_uncles()` so all callers are protected uniformly. The same fix is needed in `get_transactions_proof.rs` line 119–121, which has an identical pattern. [7](#0-6) 

---

### Proof of Concept

Unit test sketch (mock `ChainStore`):

```rust
// Mock store: is_main_chain → true, get_block_header → Some, get_block_uncles → None
// (simulates a frozen block with no freezer reference in the snapshot)
let result = std::panic::catch_unwind(|| {
    executor::block_on(process.execute())
});
assert!(result.is_err(), "execute() must panic on frozen block hash");
```

A real integration test: enable the freezer, mine past `THRESHOLD_EPOCH`, wait for `wipe_out_frozen_data` to run, then send a `GetBlocksProof` P2P message containing any block hash with number `< freezer.number()`. The node process will abort.

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

**File:** shared/src/shared.rs (L220-250)
```rust
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

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L81-90)
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
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L119-121)
```rust
            let uncles = snapshot
                .get_block_uncles(&block_hash)
                .expect("block uncles must be stored");
```
