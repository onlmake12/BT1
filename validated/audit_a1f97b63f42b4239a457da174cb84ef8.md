### Title
`migrate_transaction_info` Reads From Wrong Column During `ChangeMoleculeTableToStruct` Migration, Leaving `COLUMN_TRANSACTION_INFO` Uncorrected and Corrupted — (`File: util/migrate/src/migrations/table_to_struct.rs`)

---

### Summary

The `ChangeMoleculeTableToStruct` database migration, which converts Molecule table-encoded data to struct-encoded data, contains a copy-paste error in its `migrate_transaction_info` sub-function. The function is supposed to traverse `COLUMN_TRANSACTION_INFO` and rewrite entries in the new format, but it instead traverses `COLUMN_UNCLES`. This is the direct CKB analog of the reported vulnerability class: an upgrade/migration operation that processes some state correctly but silently omits a critical state column, leaving it in a broken state after the migration completes.

---

### Finding Description

In `util/migrate/src/migrations/table_to_struct.rs`, the `ChangeMoleculeTableToStruct` migration defines four sub-functions to reformat data in four separate columns. Three of them correctly traverse their own column:

- `migrate_header` → traverses `COLUMN_BLOCK_HEADER` ✓
- `migrate_uncles` → traverses `COLUMN_UNCLES` ✓
- `migrate_epoch_ext` → traverses `COLUMN_EPOCH` ✓

But `migrate_transaction_info` contains a copy-paste bug:

```rust
fn migrate_transaction_info(&self, db: &RocksDB) -> Result<()> {
    const TRANSACTION_INFO_SIZE: usize = 52;
    ...
    let (_count, nk) =
        db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
    //             ^^^^^^^^^^^^^ BUG: should be COLUMN_TRANSACTION_INFO
    ...
}
```

It traverses `COLUMN_UNCLES` a second time, not `COLUMN_TRANSACTION_INFO`. The closure `transaction_info_migration` then writes the uncle data (stripped of 16 bytes) into `COLUMN_TRANSACTION_INFO` keyed by uncle block hashes. The actual `COLUMN_TRANSACTION_INFO` entries — keyed by transaction hash and mapping each transaction to its containing block — are never read and never reformatted.

The result after migration:

| Column | Expected state | Actual state |
|---|---|---|
| `COLUMN_TRANSACTION_INFO` | All entries in new struct format | Entries from `COLUMN_UNCLES` written with uncle-block-hash keys; original tx-hash-keyed entries remain in old table format | [1](#0-0) 

The top-level `migrate` function calls all four sub-functions and then commits, giving no indication that `COLUMN_TRANSACTION_INFO` was silently skipped: [2](#0-1) 

---

### Impact Explanation

`COLUMN_TRANSACTION_INFO` is the index that maps every committed transaction hash to its block location (block hash, block number, transaction index within the block). After this migration runs on a pre-v0.35.0 database:

1. **All transaction-by-hash lookups silently fail.** The column now contains uncle-block-hash keys, not transaction-hash keys. Any call to `get_transaction_info` for a real transaction returns `None`.
2. **RPC endpoints are broken.** `get_transaction`, `get_transaction_proof`, `get_transaction_and_witness_proof`, and any endpoint that resolves a tx hash to a block will return null or error for every historical transaction.
3. **Garbage data is injected.** Uncle block data (stripped of 16 bytes) is written into `COLUMN_TRANSACTION_INFO` under uncle block hashes. If any code path happens to query the column with a key that collides with an uncle hash, it receives malformed data. [1](#0-0) 

---

### Likelihood Explanation

The migration is triggered automatically when a node binary is upgraded and the stored database version is older than `20200703124523` (v0.35.0). Any node operator who:

- Kept a database from before v0.35.0 and upgrades directly to a modern binary, **or**
- Restores a backup from that era and runs a current binary

will silently corrupt their `COLUMN_TRANSACTION_INFO`. The migration succeeds (returns `Ok(db)`) with no error, so the operator has no indication anything went wrong. The node will start, sync, and appear healthy until a transaction lookup is attempted.

The likelihood is low in practice because v0.35.0 was released in 2020 and most production nodes have long since migrated. However, the bug is still present in the production codebase and will affect any node that encounters this upgrade path. [3](#0-2) 

---

### Recommendation

Change `COLUMN_UNCLES` to `COLUMN_TRANSACTION_INFO` in the `traverse` call inside `migrate_transaction_info`:

```diff
- let (_count, nk) =
-     db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
+ let (_count, nk) =
+     db.traverse(COLUMN_TRANSACTION_INFO, &mut transaction_info_migration, mode, LIMIT)?;
```

Additionally, add a unit test that pre-populates both `COLUMN_UNCLES` and `COLUMN_TRANSACTION_INFO` with old-format data, runs the migration, and asserts that `COLUMN_TRANSACTION_INFO` entries are correctly reformatted and that no uncle-hash-keyed entries appear in `COLUMN_TRANSACTION_INFO`. [1](#0-0) 

---

### Proof of Concept

The bug is directly visible by inspection. The `migrate_transaction_info` function declares `TRANSACTION_INFO_SIZE = 52` and writes to `COLUMN_TRANSACTION_INFO`, but the `db.traverse` call on line 93 passes `COLUMN_UNCLES` — the same column already traversed by `migrate_uncles` on line 66. The `COLUMN_TRANSACTION_INFO` constant imported at the top of the file is never passed to any `traverse` call. [4](#0-3) [5](#0-4)

### Citations

**File:** util/migrate/src/migrations/table_to_struct.rs (L1-10)
```rust
use ckb_db::{Direction, IteratorMode, Result, RocksDB};
use ckb_db_migration::{Migration, ProgressBar, ProgressStyle};
use ckb_db_schema::{
    COLUMN_BLOCK_HEADER, COLUMN_EPOCH, COLUMN_META, COLUMN_TRANSACTION_INFO, COLUMN_UNCLES,
    META_CURRENT_EPOCH_KEY,
};
use std::sync::Arc;

pub struct ChangeMoleculeTableToStruct;

```

**File:** util/migrate/src/migrations/table_to_struct.rs (L63-101)
```rust
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
