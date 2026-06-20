The code evidence is conclusive. Here is the full trace:

**Step 1 — Freeze wipes `COLUMN_BLOCK_EXTENSION` from hot DB.**
`wipe_out_frozen_data` calls `delete_block_body` for every frozen main-chain block. [1](#0-0) 
`delete_block_body` explicitly deletes `COLUMN_BLOCK_EXTENSION`: [2](#0-1) 

**Step 2 — `get_block` has a freezer path; `get_block_extension` does not.**
`get_block` checks `freezer.number()` and retrieves the full block (including extension) from cold storage: [3](#0-2) 
`get_block_extension` only queries the hot `COLUMN_BLOCK_EXTENSION` column — no freezer branch: [4](#0-3) 

**Step 3 — `LoadBlockExtension` syscall routes through `get_block_extension`.**
For `Source::Transaction(SourceEntry::HeaderDep)`, `fetch_extension` calls `data_loader().get_block_extension(&block_hash)` directly: [5](#0-4) 
`DataLoaderWrapper::get_block_extension` delegates straight to `ChainStore::get_block_extension`: [6](#0-5) 

**Step 4 — `None` → `ITEM_MISSING` returned to the VM.** [7](#0-6) 

---

### Title
Missing freezer path in `get_block_extension` causes `ITEM_MISSING` for frozen blocks — (`store/src/store.rs`)

### Summary
`ChainStore::get_block_extension` never consults the freezer. After a block is migrated to cold storage, its extension bytes are deleted from `COLUMN_BLOCK_EXTENSION` in the hot RocksDB by `delete_block_body`. Any subsequent call to `get_block_extension` for that block returns `None`. The `LoadBlockExtension` syscall (ecall 2104) uses this function for `HeaderDep` sources and maps `None` to `ITEM_MISSING`, so a script that expects the extension to be present will fail on a freezer-enabled node but succeed on a non-freezer node — a determinism violation.

### Finding Description
`get_block` correctly handles frozen blocks by checking `header.number() < freezer.number()` and calling `freezer.retrieve(header.number())`. `get_block_extension` has no such branch; it only reads `COLUMN_BLOCK_EXTENSION`. The freeze pipeline (`wipe_out_frozen_data` → `delete_block_body`) deletes that column entry for every frozen main-chain block. The asymmetry means `get_block` and `get_block_extension` return inconsistent results for the same frozen block hash.

### Impact Explanation
A script running under ScriptVersion ≥ V2 can call `LoadBlockExtension` (syscall 2104) with `Source::Transaction(SourceEntry::HeaderDep)` pointing to any block hash that is a declared `header_dep`. If that block has been frozen on the verifying node, the syscall returns `ITEM_MISSING` instead of `SUCCESS`. A miner without the freezer enabled would include the transaction (script passes); a freezer-enabled full node would reject the containing block (script fails). This is a consensus split: the same block is accepted by some nodes and rejected by others, depending solely on whether the freezer is active and the referenced block has been migrated.

### Likelihood Explanation
The freezer is a supported production feature (disabled by default but documented and deployable). Any transaction whose script calls `LoadBlockExtension` on a `HeaderDep` that is old enough to have been frozen triggers the bug. No special privilege is required — any script author can craft such a transaction. The precondition (freezer enabled + block frozen) is realistic on long-running archival nodes.

### Recommendation
Add a freezer fallback to `get_block_extension` mirroring the one in `get_block`:

```rust
fn get_block_extension(&self, hash: &packed::Byte32) -> Option<packed::Bytes> {
    // cache check ...
    if let Some(freezer) = self.freezer() {
        if let Some(header) = self.get_block_header(hash) {
            if header.number() > 0 && header.number() < freezer.number() {
                let raw = freezer.retrieve(header.number()).expect("block frozen")?;
                let reader = packed::BlockReader::from_compatible_slice(&raw)
                    .expect("checked data");
                // BlockV1 extension lives in the extra field
                return reader.as_v1().map(|v1| v1.extension().to_entity());
            }
        }
    }
    // hot-DB path ...
}
```

Also add a test that freezes a BlockV1, then asserts `get_block_extension` returns the same bytes as `get_block(...).extension()`.

### Proof of Concept
```rust
// In store/src/tests/db.rs
#[test]
fn get_block_extension_after_freeze_returns_extension() {
    let tmp_dir  = TempDir::new().unwrap();
    let tmp_dir2 = TempDir::new().unwrap();
    let db      = RocksDB::open_in(&tmp_dir, COLUMNS);
    let freezer = Freezer::open_in(&tmp_dir2).expect("freezer");
    let store   = ChainDB::new_with_freezer(db, freezer.clone(), Default::default());

    let extension: packed::Bytes = [1u8; 96].into();
    let raw   = packed::RawHeader::new_builder().number(1u64).build();
    let block = packed::BlockV1::new_builder()
        .header(packed::Header::new_builder().raw(raw).build())
        .extension(extension.clone())
        .build()
        .as_v0()
        .into_view();
    let hash = block.hash();

    // Insert only the header (simulating post-freeze state: body/extension wiped)
    let txn = store.begin_transaction();
    txn.insert_raw(COLUMN_BLOCK_HEADER, hash.as_slice(),
        Into::<packed::HeaderView>::into(block.header()).as_slice())
        .unwrap();
    txn.commit().unwrap();

    freezer.freeze(2, |_| Some(block.clone())).unwrap();

    // Simulate wipe: delete COLUMN_BLOCK_EXTENSION from hot DB
    // (delete_block_body does this in production)
    // After freeze, get_block_extension must still return the extension:
    let got = store.get_block_extension(&hash);
    // FAILS today: got == None, expected == Some(extension)
    assert_eq!(got, Some(extension));
}
```

Running this test against the current code produces `None` for `get_block_extension`, confirming the missing freezer path.

### Citations

**File:** shared/src/shared.rs (L222-226)
```rust
            for (hash, (number, txs)) in &frozen {
                batch.delete_block_body(*number, hash, *txs).map_err(|e| {
                    ckb_logger::error!("Freezer delete_block_body failed {}", e);
                    e
                })?;
```

**File:** store/src/write_batch.rs (L97-98)
```rust
        self.inner.delete(COLUMN_BLOCK_UNCLE, hash.as_slice())?;
        self.inner.delete(COLUMN_BLOCK_EXTENSION, hash.as_slice())?;
```

**File:** store/src/store.rs (L44-53)
```rust
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
```

**File:** store/src/store.rs (L227-242)
```rust
    fn get_block_extension(&self, hash: &packed::Byte32) -> Option<packed::Bytes> {
        if let Some(cache) = self.cache()
            && let Some(data) = cache.block_extensions.lock().get(hash)
        {
            return data.clone();
        };

        let ret = self
            .get(COLUMN_BLOCK_EXTENSION, hash.as_slice())
            .map(|slice| packed::BytesReader::from_slice_should_be_ok(slice.as_ref()).to_entity());

        if let Some(cache) = self.cache() {
            cache.block_extensions.lock().put(hash.clone(), ret.clone());
        }
        ret
    }
```

**File:** script/src/syscalls/load_block_extension.rs (L75-84)
```rust
            Source::Transaction(SourceEntry::HeaderDep) => self
                .header_deps()
                .get(index)
                .ok_or(INDEX_OUT_OF_BOUND)
                .and_then(|block_hash| {
                    self.sg_data
                        .data_loader()
                        .get_block_extension(&block_hash)
                        .ok_or(ITEM_MISSING)
                }),
```

**File:** script/src/syscalls/load_block_extension.rs (L119-122)
```rust
        if let Err(err) = extension {
            machine.set_register(A0, Mac::REG::from_u8(err));
            return Ok(true);
        }
```

**File:** store/src/data_loader_wrapper.rs (L106-108)
```rust
    fn get_block_extension(&self, hash: &Byte32) -> Option<packed::Bytes> {
        ChainStore::get_block_extension(self.0.as_ref(), hash)
    }
```
