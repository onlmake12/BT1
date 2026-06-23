### Title
Wrong Source Column in `migrate_transaction_info` Corrupts `COLUMN_TRANSACTION_INFO` During Database Migration — (File: `util/migrate/src/migrations/table_to_struct.rs`)

---

### Summary

The `ChangeMoleculeTableToStruct` migration's `migrate_transaction_info` function iterates over `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO`. As a result, uncle block data is written into the transaction-info column, while the actual transaction-info entries are never migrated out of the old Molecule table format. This is a direct analog to the LiquidityWindow V1/V2 storage-slot reordering bug: one storage region's bytes are written into a different region, causing the surviving node to misinterpret its own persistent state.

---

### Finding Description

In `util/migrate/src/migrations/table_to_struct.rs`, the function `migrate_transaction_info` is responsible for stripping the 16-byte Molecule *table* header from every entry in `COLUMN_TRANSACTION_INFO`, converting them to the leaner *struct* encoding. The closure correctly targets `COLUMN_TRANSACTION_INFO` as the **write** destination, but the **read** traversal is issued against `COLUMN_UNCLES`:

```rust
// line 93 — wrong source column
let (_count, nk) =
    db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
``` [1](#0-0) 

The correct call should traverse `COLUMN_TRANSACTION_INFO`. Because it traverses `COLUMN_UNCLES` instead, two things happen simultaneously:

1. **Uncle data is injected into `COLUMN_TRANSACTION_INFO`.** Every uncle-block entry whose byte length differs from 52 (the struct size of `TransactionInfo`) has its first 16 bytes stripped and is written into `COLUMN_TRANSACTION_INFO` under the uncle's block-hash key.
2. **Actual `COLUMN_TRANSACTION_INFO` entries are never migrated.** They remain in the old Molecule *table* format (with a 16-byte size-offset header), which the post-migration reader no longer expects.

The migration is registered as `ChangeMoleculeTableToStruct` and has been present since v0.35.0: [2](#0-1) 

The migration framework stamps the new DB version after the migration completes, so the node will not re-run it: [3](#0-2) 

Once the version is stamped, the corruption is permanent unless the operator manually repairs the database.

---

### Impact Explanation

After the migration runs, every read of `COLUMN_TRANSACTION_INFO` by transaction hash returns the old Molecule table-encoded bytes. The first 16 bytes are a size/offset table, not payload. Code that now expects the struct layout will misparse:

- `block_hash` (32 bytes) — read from the wrong offset, returns garbage
- `block_number` (8 bytes) — wrong offset
- `block_epoch` (8 bytes) — wrong offset
- `index` (4 bytes) — wrong offset

Additionally, spurious entries keyed by block hashes (from uncle data) pollute `COLUMN_TRANSACTION_INFO`.

Concrete downstream effects:
- **RPC `get_transaction`** returns a wrong or unparseable block location for any transaction whose info was stored before v0.35.0.
- **`get_transaction_info`** and related store methods return corrupted `TransactionInfo`, breaking any caller that relies on the block-number or block-hash fields.
- Any consensus or tx-pool path that cross-checks a transaction's inclusion block via `COLUMN_TRANSACTION_INFO` operates on corrupted state. [4](#0-3) 

---

### Likelihood Explanation

The migration is triggered automatically on node startup (or via `ckb migrate`) whenever the stored DB version is older than the `ChangeMoleculeTableToStruct` version string. Any operator upgrading from a pre-v0.35.0 database hits this path with no special configuration. The migration framework provides no integrity check on the migrated data, so the corruption is silent and the node proceeds normally after stamping the new version. [5](#0-4) 

---

### Recommendation

Fix the traversal source column in `migrate_transaction_info`:

```rust
// Before (wrong):
db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;

// After (correct):
db.traverse(COLUMN_TRANSACTION_INFO, &mut transaction_info_migration, mode, LIMIT)?;
```

Additionally, add a post-migration integrity check that samples a known transaction-info entry and verifies it decodes correctly as a `TransactionInfo` struct before stamping the new DB version.

---

### Proof of Concept

1. Start a CKB node with a pre-v0.35.0 database that contains at least one transaction and at least one uncle block.
2. Run `ckb migrate --force` (or start the node, which triggers fast migration automatically).
3. After migration completes, query any transaction that existed before the migration via `get_transaction` RPC.
4. Observe that the returned `block_hash` / `block_number` fields are wrong or the response fails to deserialize, because the stored bytes still carry the 16-byte Molecule table header that the post-migration reader does not account for.
5. Simultaneously, iterate `COLUMN_TRANSACTION_INFO` directly and observe entries keyed by block hashes (uncle keys) containing truncated uncle-block payloads — data that does not belong in this column at all. [1](#0-0) [6](#0-5)

### Citations

**File:** util/migrate/src/migrations/table_to_struct.rs (L77-101)
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
```

**File:** util/migrate/src/migrate.rs (L26-26)
```rust
        migrations.add_migration(Arc::new(migrations::ChangeMoleculeTableToStruct)); // since v0.35.0
```

**File:** db-migration/src/lib.rs (L333-347)
```rust
    fn check_migration_downgrade(&self, cur_version: &str) -> Result<(), Error> {
        if let Some(m) = self.migrations.values().last()
            && m.version() < cur_version
        {
            error!(
                "Database downgrade detected. \
                    The database schema version is newer than `ckb` schema version,\
                    please upgrade `ckb` to the latest version"
            );
            return Err(internal_error(
                "Database downgrade is not supported".to_string(),
            ));
        }
        Ok(())
    }
```

**File:** store/src/transaction.rs (L130-145)
```rust
impl StoreTransaction {
    /// Inserts a raw key-value pair into the specified column.
    pub fn insert_raw(&self, col: Col, key: &[u8], value: &[u8]) -> Result<(), Error> {
        self.inner.put(col, key, value)
    }

    /// Deletes a key from the specified column.
    pub fn delete(&self, col: Col, key: &[u8]) -> Result<(), Error> {
        self.inner.delete(col, key)
    }

    /// Commits the transaction, writing all changes to the database.
    pub fn commit(&self) -> Result<(), Error> {
        self.inner.commit()
    }

```

**File:** shared/src/shared_builder.rs (L73-131)
```rust
    if let Some(db) = read_only_db {
        match migrate.check(&db, true) {
            Ordering::Greater => {
                eprintln!(
                    "The database was created by a higher version CKB executable binary \n\
                     and cannot be opened by the current binary.\n\
                     Please download the latest CKB executable binary."
                );
                Err(ExitCode::Failure)
            }
            Ordering::Equal => Ok(RocksDB::open(config, COLUMNS)),
            Ordering::Less => {
                let can_run_in_background = migrate.can_run_in_background(&db);
                if migrate.require_expensive(&db, false) && !can_run_in_background {
                    eprintln!(
                        "For optimal performance, CKB recommends migrating your data into a new format.\n\
                        If you prefer to stick with the older version, \n\
                        it's important to note that they may have unfixed vulnerabilities.\n\
                        Before migrating, we strongly recommend backuping your data directory.\n\
                        To migrate, run `\"{}\" migrate -C \"{}\"` and confirm by typing \"YES\".",
                        bin_name,
                        root_dir.display()
                    );
                    Err(ExitCode::Failure)
                } else if can_run_in_background {
                    info!("process migrations in background ...");
                    let db = RocksDB::open(config, COLUMNS);
                    migrate.migrate(db.clone(), true).map_err(|err| {
                        eprintln!("Run error: {err:?}");
                        ExitCode::Failure
                    })?;
                    Ok(db)
                } else {
                    info!("Processing fast migrations ...");

                    let bulk_load_db_db = migrate.open_bulk_load_db().map_err(|e| {
                        eprintln!("Migration error {e}");
                        ExitCode::Failure
                    })?;

                    if let Some(db) = bulk_load_db_db {
                        migrate.migrate(db, false).map_err(|err| {
                            eprintln!("Run error: {err:?}");
                            ExitCode::Failure
                        })?;
                    }

                    Ok(RocksDB::open(config, COLUMNS))
                }
            }
        }
    } else {
        let db = RocksDB::open(config, COLUMNS);
        migrate.init_db_version(&db).map_err(|e| {
            eprintln!("Migrate init_db_version error {e}");
            ExitCode::Failure
        })?;
        Ok(db)
    }
```

**File:** util/migrate/src/migrations/mod.rs (L9-19)
```rust
mod table_to_struct;

pub use add_block_extension_cf::AddBlockExtensionColumnFamily;
pub use add_block_filter::AddBlockFilterColumnFamily;
pub use add_block_filter_hash::AddBlockFilterHash;
pub use add_chain_root_mmr::AddChainRootMMR;
pub use add_extra_data_hash::AddExtraDataHash;
pub use add_number_hash_mapping::AddNumberHashMapping;
pub use cell::CellMigration;
pub use set_2019_block_cycle_zero::BlockExt2019ToZero;
pub use table_to_struct::ChangeMoleculeTableToStruct;
```
