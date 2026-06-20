Now I have all the information needed for a complete analysis. Let me trace the full execution path.

**Key facts established:**

1. `build_cell_meta_from_reader` always sets `mem_cell_data = None` and `mem_cell_data_hash = None` for cells loaded from DB. [1](#0-0) 

2. Cell deps are resolved with `eager_load = false`, so `mem_cell_data_hash` is never populated at resolution time. [2](#0-1) 

3. `load_cell_data_hash` checks `mem_cell_data_hash` first; if `None`, falls back to `get_cell_data_hash` (RocksDB read). [3](#0-2) 

4. `get_cell_data_hash` checks a 128-entry LRU cache, then reads `COLUMN_CELL_DATA_HASH` from RocksDB. [4](#0-3) 

5. `cell_data_hash` LRU cache size = `cell_data_cache_size = 128` by default. [5](#0-4) 

6. `TxData::new()` pre-loads all cell dep data hashes into the LRU cache — but with up to 2048 cell deps, the first 1920 entries are evicted before the loop finishes. [6](#0-5) 

7. `MAX_DEP_EXPANSION_LIMIT = 2048`. [7](#0-6) 

8. Cycle charge for `CellField::DataHash` = `transferred_byte_cycles(32) = ceil(32/4) = 8` cycles. [8](#0-7) 

9. `max_tx_verify_cycles = 70,000,000`. [9](#0-8) 

**Critical interaction:** `TxData::new()` iterates all 2048 resolved cell deps calling `load_cell_data_hash`, which populates the LRU cache. But since the cache holds only 128 entries, after the loop completes, only the last 128 cell deps remain cached. The first 1920 are evicted. When the script then accesses those evicted indices via `CellField::DataHash`, each triggers a fresh RocksDB read at 8 cycles. In a round-robin access pattern over 2048 indices with a 128-entry cache, every access is a cache miss, yielding up to `70,000,000 / 8 = 8,750,000` DB reads per transaction.

---

### Title
Cycle Cost for `CellField::DataHash` Does Not Account for RocksDB I/O When `mem_cell_data_hash` Is None — (`script/src/syscalls/load_cell.rs`)

### Summary
The `LOAD_CELL_BY_FIELD` syscall with `CellField::DataHash` charges only `transferred_byte_cycles(32) = 8` cycles regardless of whether the hash is served from memory or requires a RocksDB point lookup. An attacker can craft a transaction with up to 2048 resolved cell deps (via dep groups) and loop over their indices in a pattern that continuously evicts the 128-entry LRU cache, triggering up to ~8.75 million RocksDB reads per transaction at 8 cycles each.

### Finding Description
In `load_by_field`, the `CellField::DataHash` arm calls `self.tx_info().data_loader.load_cell_data_hash(cell)`. [10](#0-9) 

`load_cell_data_hash` checks `mem_cell_data_hash` first; if `None`, it falls back to `get_cell_data_hash(&cell.out_point)`. [3](#0-2) 

For all cell deps resolved from the chain store, `build_cell_meta_from_reader` always produces `mem_cell_data_hash: None`. [11](#0-10) 

`get_cell_data_hash` checks a 128-entry LRU cache, then issues a RocksDB `COLUMN_CELL_DATA_HASH` read on a miss. [12](#0-11) 

After the syscall returns, the only cycle charge is `transferred_byte_cycles(len)` where `len = 32`, yielding 8 cycles — with no additional charge for the DB I/O. [13](#0-12) 

`TxData::new()` pre-loads all cell dep hashes into the LRU cache, but with 2048 cell deps and a 128-entry cache, the first 1920 entries are evicted before the pre-load loop finishes. [6](#0-5) 

### Impact Explanation
With `MAX_DEP_EXPANSION_LIMIT = 2048` and a 128-entry LRU cache, a script looping over all 2048 cell dep indices in round-robin order triggers a cache miss on every access (2048 > 128). At 8 cycles per call and a 70M cycle budget, the script can issue approximately **8.75 million RocksDB point reads** per transaction. Each read fetches 32 bytes from `COLUMN_CELL_DATA_HASH`. This creates a severe I/O-to-cycle amplification ratio: the cycle cost implies ~0.7 seconds of CPU work, but the actual I/O load can be orders of magnitude higher, stalling verification threads and congesting the network.

### Likelihood Explanation
The attacker needs to own live cells to use as cell deps, but dep groups allow referencing 2048 resolved cell deps with a compact transaction (a single dep group cell dep pointing to a cell containing 2048 out-points). The transaction fee is proportional to transaction size, not to the number of DB reads triggered. This makes the attack economically viable: a small, cheap transaction can impose large I/O costs on every verifying node.

### Recommendation
Charge a fixed base cost per `CellField::DataHash` syscall invocation that accounts for the potential DB lookup, independent of the data size returned. A flat cost of at least several hundred cycles (comparable to other I/O-bound syscalls) should be added when `mem_cell_data_hash` is `None` and a storage fallback is required. Alternatively, eagerly populate `mem_cell_data_hash` for all resolved cell deps before script execution begins (i.e., change `eager_load` to `true` for cell deps, or add a dedicated pre-loading pass that stores the hash back into the `CellMeta`), so the DB read is paid once at resolution time and the syscall always serves from memory.

### Proof of Concept
1. Deploy a dep-group cell whose data encodes 2048 out-points of existing live cells.
2. Construct a transaction whose single cell dep references that dep group; it resolves to 2048 `CellMeta` entries all with `mem_cell_data_hash = None`.
3. The lock script executes a tight loop:
   ```c
   for (uint64_t round = 0; ; round++) {
     for (uint64_t i = 0; i < 2048; i++) {
       uint8_t buf[32]; uint64_t len = 32;
       ckb_load_cell_by_field(buf, &len, 0, i, CKB_SOURCE_CELL_DEP, CKB_CELL_FIELD_DATA_HASH);
     }
   }
   ```
4. Differential measurement: record wall-clock time for the same loop with `mem_cell_data_hash` pre-populated (in-memory path) vs. `None` (DB path). The DB path will be dramatically slower despite consuming identical cycles, confirming the I/O cost is not reflected in the cycle charge.

### Citations

**File:** store/src/store.rs (L389-412)
```rust
    fn get_cell_data_hash(&self, out_point: &OutPoint) -> Option<packed::Byte32> {
        let key = out_point.to_cell_key();
        if let Some(cache) = self.cache()
            && let Some(cached) = cache.cell_data_hash.lock().get(&key)
        {
            return Some(cached.clone());
        };

        let ret = self.get(COLUMN_CELL_DATA_HASH, &key).map(|raw| {
            if !raw.as_ref().is_empty() {
                packed::Byte32Reader::from_slice_should_be_ok(raw.as_ref()).to_entity()
            } else {
                packed::Byte32::zero()
            }
        });

        if let Some(cache) = self.cache() {
            ret.inspect(|cached| {
                cache.cell_data_hash.lock().put(key, cached.clone());
            })
        } else {
            ret
        }
    }
```

**File:** store/src/store.rs (L579-592)
```rust
fn build_cell_meta_from_reader(out_point: OutPoint, reader: packed::CellEntryReader) -> CellMeta {
    CellMeta {
        out_point,
        cell_output: reader.output().to_entity(),
        transaction_info: Some(TransactionInfo {
            block_number: reader.block_number().into(),
            block_hash: reader.block_hash().to_entity(),
            block_epoch: reader.block_epoch().into(),
            index: reader.index().into(),
        }),
        data_bytes: reader.data_size().into(),
        mem_cell_data: None,
        mem_cell_data_hash: None,
    }
```

**File:** util/types/src/core/cell.rs (L33-33)
```rust
const MAX_DEP_EXPANSION_LIMIT: usize = 2048;
```

**File:** util/types/src/core/cell.rs (L786-789)
```rust
                    resolved_dep_groups,
                    false, // don't eager_load data
                    &mut remaining_dep_slots,
                )?;
```

**File:** traits/src/cell_data_provider.rs (L18-23)
```rust
    fn load_cell_data_hash(&self, cell: &CellMeta) -> Option<Byte32> {
        cell.mem_cell_data_hash
            .as_ref()
            .map(ToOwned::to_owned)
            .or_else(|| self.get_cell_data_hash(&cell.out_point))
    }
```

**File:** store/src/cache.rs (L40-40)
```rust
            cell_data_hash: Mutex::new(LruCache::new(config.cell_data_cache_size)),
```

**File:** script/src/types.rs (L701-706)
```rust
        for (i, cell_meta) in resolved_cell_deps.iter().enumerate() {
            let data_hash = data_loader
                .load_cell_data_hash(cell_meta)
                .expect("cell data hash");
            let lazy = LazyData::from_cell_meta(cell_meta);
            binaries_by_data_hash.insert(data_hash.to_owned(), (i, lazy.to_owned()));
```

**File:** script/src/cost_model.rs (L7-12)
```rust
pub const BYTES_PER_CYCLE: u64 = 4;

/// Calculates how many cycles spent to load the specified number of bytes.
pub fn transferred_byte_cycles(bytes: u64) -> u64 {
    // Compiler will optimize the divisin here to shifts.
    bytes.div_ceil(BYTES_PER_CYCLE)
```

**File:** resource/ckb.toml (L215-215)
```text
max_tx_verify_cycles = 70_000_000
```

**File:** script/src/syscalls/load_cell.rs (L111-116)
```rust
            CellField::DataHash => {
                if let Some(bytes) = self.tx_info().data_loader.load_cell_data_hash(cell) {
                    (SUCCESS, store_data(machine, &bytes.as_bytes())?)
                } else {
                    (ITEM_MISSING, 0)
                }
```

**File:** script/src/syscalls/load_cell.rs (L191-191)
```rust
        machine.add_cycles_no_checking(transferred_byte_cycles(len))?;
```
