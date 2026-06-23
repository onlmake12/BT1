I found the exact analog. In `util/migrate/src/migrations/table_to_struct.rs`, the `migrate_transaction_info` function traverses `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO`, meaning transaction info is never actually migrated.

### Title
`migrate_transaction_info` Traverses Wrong Column, Leaving `COLUMN_TRANSACTION_INFO` Unmigrated — (`File: util/migrate/src/migrations/table_to_struct.rs`)

---

### Summary

The `ChangeMoleculeTableToStruct` migration's `migrate_transaction_info` function traverses `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO`. As a result, every `TransactionInfo` record written in the old molecule Table encoding is never converted to the new Struct encoding. Any node that ran this migration while holding pre-v0.35.0 data retains stale, mis-encoded transaction info for every historical transaction, causing corrupted reads for all downstream consumers.

---

### Finding Description

`ChangeMoleculeTableToStruct` (version `20200703124523`, shipped in v0.35.0) rewrites four column families from molecule's variable-length Table layout to fixed-size Struct layout. Three of the four sub-migrations correctly traverse their own column:

| Sub-migration | Column traversed | Column written |
|---|---|---|
| `migrate_header` | `COLUMN_BLOCK_HEADER` | `COLUMN_BLOCK_HEADER` ✓ |
| `migrate_uncles` | `COLUMN_UNCLES` | `COLUMN_UNCLES` ✓ |
| **`migrate_transaction_info`** | **`COLUMN_UNCLES`** ← **wrong** | `COLUMN_TRANSACTION_INFO` ✗ |
| `migrate_epoch_ext` | `COLUMN_EPOCH` | `COLUMN_EPOCH` ✓ |

The defective function:

```rust
// util/migrate/src/migrations/table_to_struct.rs  lines 77-102
fn migrate_transaction_info(&self, db: &RocksDB) -> Result<()> {
    const TRANSACTION_INFO_SIZE: usize = 52;
    let mut next_key = vec![0];
    while !next_key.is_empty() {
        let mut wb = db.new_write_batch();
        let mut transaction_info_migration = |key: &[u8], value: &[u8]| -> Result<()> {
            if value.len() != TRANSACTION_INFO_SIZE {
                wb.put(COLUMN_TRANSACTION_INFO, key, &value[16..])?;  // writes to correct col
            }
            Ok(())
        };
        let mode = self.mode(&next_key);
        let (_count, nk) =
            db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
        //              ^^^^^^^^^^^^^ BUG: must be COLUMN_TRANSACTION_INFO
        next_key = nk;
        ...
    }
    Ok(())
}
``` [1](#0-0) 

The closure correctly targets `COLUMN_TRANSACTION_INFO` for writes, but the `db.traverse` call feeds it records from `COLUMN_UNCLES`. Because uncle header records are already 240 bytes (the correct post-migration size for headers, not 52), the `value.len() != TRANSACTION_INFO_SIZE` guard is always true for uncle records, so the closure fires — but it writes uncle-keyed data into `COLUMN_TRANSACTION_INFO` under uncle hashes, not transaction hashes. The actual `COLUMN_TRANSACTION_INFO` entries (keyed by tx hash) are never visited and remain in the old 68-byte Table format.

The old Table format for `TransactionInfo` is:

```
[0..4]   total_size (u32 LE)
[4..8]   offset of block_number
[8..12]  offset of block_epoch
[12..16] offset of key
[16..24] block_number  (u64)
[24..32] block_epoch   (u64)
[32..68] key           (TransactionKey, 36 bytes)
```

The new Struct format (`TransactionInfoReader::TOTAL_SIZE = 52`) is:

```
[0..8]   block_number
[8..16]  block_epoch
[16..52] key
``` [2](#0-1) 

After the migration runs, `get_transaction_info` deserializes old 68-byte records using fixed Struct offsets:

```rust
// store/src/store.rs  lines 307-313
fn get_transaction_info(&self, hash: &packed::Byte32) -> Option<TransactionInfo> {
    self.get(COLUMN_TRANSACTION_INFO, hash.as_slice())
        .map(|slice| {
            let reader = packed::TransactionInfoReader::from_slice_should_be_ok(slice.as_ref());
            Into::<TransactionInfo>::into(reader)
        })
}
``` [3](#0-2) 

`from_slice_should_be_ok` does not validate total length; it wraps the raw slice. The field accessors then read at wrong offsets:

- `block_number()` → bytes `[0..8]` → reads the 4-byte `total_size` + first 4 bytes of offset table (garbage)
- `block_epoch()` → bytes `[8..16]` → reads remaining offset bytes (garbage)
- `key()` → bytes `[16..52]` → reads the actual `block_number` + `block_epoch` + first 4 bytes of `key` (shifted, wrong)

Every historical transaction's info is therefore silently corrupted.

---

### Impact Explanation

1. **`get_transaction` / `get_transaction_with_info` RPC** — returns a corrupted `block_hash` derived from the mangled `key` field. The subsequent `get(COLUMN_BLOCK_BODY, tx_info.key().as_slice())` lookup uses a wrong key and returns `None`, causing `get_transaction` to silently return `None` for any pre-migration transaction. RPC callers (wallets, explorers, dApps) receive incorrect "transaction not found" responses for confirmed transactions. [4](#0-3) 

2. **`transaction_exists`** — the key still exists in the column (just with wrong value bytes), so existence checks return `true`. However, `get_transaction_with_info` fails to resolve the actual transaction body, breaking any code path that calls both. [5](#0-4) 

3. **tx-pool admission** — `tx-pool/src/pool.rs` calls `transaction_exists` to guard against re-submission of confirmed transactions. Because the key is present (existence is unaffected), this guard still fires correctly. However, any pool logic that subsequently calls `get_transaction_with_info` to inspect the confirmed tx will receive `None` and may behave incorrectly.

4. **Reward / DAO verification** — any verifier that resolves a historical transaction by hash through `get_transaction` will silently fail to find it, potentially causing block verification to reject valid blocks or accept invalid ones depending on error-handling paths.

---

### Likelihood Explanation

The migration is triggered automatically on node startup when the database version is behind, or explicitly via `ckb migrate`. Any operator who upgraded a node from a version prior to v0.35.0 (which used the Table encoding) to v0.35.0 or later would have run this migration and would have a corrupted `COLUMN_TRANSACTION_INFO`. The migration path is a standard, documented upgrade procedure — no special configuration or attacker action is required. The bug is deterministic: every pre-migration `TransactionInfo` record is affected. [6](#0-5) 

---

### Recommendation

Change line 93 of `util/migrate/src/migrations/table_to_struct.rs` from:

```rust
db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
```

to:

```rust
db.traverse(COLUMN_TRANSACTION_INFO, &mut transaction_info_migration, mode, LIMIT)?;
```

This mirrors the correct pattern used by `migrate_header` (traverses `COLUMN_BLOCK_HEADER`) and `migrate_uncles` (traverses `COLUMN_UNCLES`). [7](#0-6) 

Because the migration version is already stamped in the database for affected nodes, a new migration step (with a new version string) must be added to re-scan `COLUMN_TRANSACTION_INFO` and rewrite any remaining 68-byte Table-format entries to the 52-byte Struct format.

---

### Proof of Concept

1. Start a CKB node on any version prior to v0.35.0 and sync at least one block (so `COLUMN_TRANSACTION_INFO` has entries in Table format, i.e., 68 bytes each).
2. Upgrade the binary to v0.35.0+ and run `ckb migrate` (or start the node, which auto-migrates).
3. The migration completes and stamps version `20200703124523`.
4. Query any pre-migration transaction via RPC: `get_transaction(<tx_hash>)`.
5. Observe `null` returned despite the transaction being confirmed on-chain, because `get_transaction_with_info` resolves a corrupted `block_hash` from the mangled `key` field and fails to find the block body.

The root cause is confirmed at: [1](#0-0) 

compared against the correct pattern: [8](#0-7)

### Citations

**File:** util/migrate/src/migrations/table_to_struct.rs (L23-75)
```rust
    fn migrate_header(&self, db: &RocksDB) -> Result<()> {
        const HEADER_SIZE: usize = 240;
        let mut next_key = vec![0];
        while !next_key.is_empty() {
            let mut wb = db.new_write_batch();
            let mut header_view_migration = |key: &[u8], value: &[u8]| -> Result<()> {
                // (1 total size field + 2 fields) * 4 byte per field
                if value.len() != HEADER_SIZE {
                    wb.put(COLUMN_BLOCK_HEADER, key, &value[12..])?;
                }

                Ok(())
            };

            let mode = self.mode(&next_key);

            let (_count, nk) =
                db.traverse(COLUMN_BLOCK_HEADER, &mut header_view_migration, mode, LIMIT)?;
            next_key = nk;

            if !wb.is_empty() {
                db.write(&wb)?;
                wb.clear()?;
            }
        }

        Ok(())
    }

    fn migrate_uncles(&self, db: &RocksDB) -> Result<()> {
        const HEADER_SIZE: usize = 240;
        let mut next_key = vec![0];
        while !next_key.is_empty() {
            let mut wb = db.new_write_batch();
            let mut uncles_migration = |key: &[u8], value: &[u8]| -> Result<()> {
                // (1 total size field + 2 fields) * 4 byte per field
                if value.len() != HEADER_SIZE {
                    wb.put(COLUMN_UNCLES, key, &value[12..])?;
                }
                Ok(())
            };

            let mode = self.mode(&next_key);
            let (_count, nk) = db.traverse(COLUMN_UNCLES, &mut uncles_migration, mode, LIMIT)?;
            next_key = nk;

            if !wb.is_empty() {
                db.write(&wb)?;
                wb.clear()?;
            }
        }
        Ok(())
    }
```

**File:** util/migrate/src/migrations/table_to_struct.rs (L77-102)
```rust
    fn migrate_transaction_info(&self, db: &RocksDB) -> Result<()> {
        const TRANSACTION_INFO_SIZE: usize = 52;
        let mut next_key = vec![0];
        while !next_key.is_empty() {
            let mut wb = db.new_write_batch();
            let mut transaction_info_migration = |key: &[u8], value: &[u8]| -> Result<()> {
                // (1 total size field + 3 fields) * 4 byte per field
                if value.len() != TRANSACTION_INFO_SIZE {
                    wb.put(COLUMN_TRANSACTION_INFO, key, &value[16..])?;
                }
                Ok(())
            };

            let mode = self.mode(&next_key);

            let (_count, nk) =
                db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
            next_key = nk;

            if !wb.is_empty() {
                db.write(&wb)?;
                wb.clear()?;
            }
        }
        Ok(())
    }
```

**File:** util/migrate/src/migrations/table_to_struct.rs (L132-181)
```rust
impl Migration for ChangeMoleculeTableToStruct {
    fn migrate(
        &self,
        db: RocksDB,
        pb: Arc<dyn Fn(u64) -> ProgressBar + Send + Sync>,
    ) -> Result<RocksDB> {
        let pb = pb(9);
        let spinner_style = ProgressStyle::default_spinner()
            .tick_chars("⠁⠂⠄⡀⢀⠠⠐⠈ ")
            .template("{prefix:.bold.dim} {spinner} {wide_msg}")
            .expect("Failed to set progress bar template");
        pb.set_style(spinner_style);

        pb.set_message("migrating: block header");
        pb.inc(1);
        self.migrate_header(&db)?;
        pb.set_message("finish: block header");
        pb.inc(1);

        pb.set_message("migrating: uncles");
        pb.inc(1);
        self.migrate_uncles(&db)?;
        pb.set_message("finish: uncles");
        pb.inc(1);

        pb.set_message("migrating: transaction info");
        pb.inc(1);
        self.migrate_transaction_info(&db)?;
        pb.set_message("finish: transaction info");
        pb.inc(1);

        pb.set_message("migrating: epoch");
        pb.inc(1);
        self.migrate_epoch_ext(&db)?;
        pb.set_message("finish: epoch");
        pb.inc(1);

        let mut wb = db.new_write_batch();
        if let Some(current_epoch) = db.get_pinned(COLUMN_META, META_CURRENT_EPOCH_KEY)?
            && current_epoch.len() != 108
        {
            wb.put(COLUMN_META, META_CURRENT_EPOCH_KEY, &current_epoch[36..])?;
        }
        db.write(&wb)?;

        pb.set_message("commit changes");
        pb.inc(1);
        pb.finish_with_message("waiting...");
        Ok(db)
    }
```

**File:** store/src/store.rs (L296-298)
```rust
    fn transaction_exists(&self, hash: &packed::Byte32) -> bool {
        self.get(COLUMN_TRANSACTION_INFO, hash.as_slice()).is_some()
    }
```

**File:** store/src/store.rs (L307-313)
```rust
    fn get_transaction_info(&self, hash: &packed::Byte32) -> Option<TransactionInfo> {
        self.get(COLUMN_TRANSACTION_INFO, hash.as_slice())
            .map(|slice| {
                let reader = packed::TransactionInfoReader::from_slice_should_be_ok(slice.as_ref());
                Into::<TransactionInfo>::into(reader)
            })
    }
```

**File:** store/src/store.rs (L316-342)
```rust
    fn get_transaction_with_info(
        &self,
        hash: &packed::Byte32,
    ) -> Option<(TransactionView, TransactionInfo)> {
        let tx_info = self.get_transaction_info(hash)?;
        if let Some(freezer) = self.freezer()
            && tx_info.block_number > 0
            && tx_info.block_number < freezer.number()
        {
            let raw_block = freezer
                .retrieve(tx_info.block_number)
                .expect("block frozen")?;
            let raw_block_reader =
                packed::BlockReader::from_compatible_slice(&raw_block).expect("checked data");
            if raw_block_reader.calc_header_hash().as_slice() == tx_info.block_hash.as_slice()
                && let Some(tx_reader) = raw_block_reader.transactions().get(tx_info.index)
                && tx_reader.calc_tx_hash().as_slice() == hash.as_slice()
            {
                return Some((tx_reader.to_entity().into_view(), tx_info));
            }
        }
        self.get(COLUMN_BLOCK_BODY, tx_info.key().as_slice())
            .map(|slice| {
                let reader = packed::TransactionViewReader::from_slice_should_be_ok(slice.as_ref());
                (reader.into(), tx_info)
            })
    }
```
