### Title
`ChangeMoleculeTableToStruct` Migration Silently Corrupts `COLUMN_TRANSACTION_INFO` Due to Wrong Column Reference - (File: `util/migrate/src/migrations/table_to_struct.rs`)

### Summary

The `migrate_transaction_info` sub-function inside the `ChangeMoleculeTableToStruct` database migration reads from `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO`. This means the actual transaction info records are never converted from the old Molecule Table format to the new Struct format. Worse, uncle data (sliced with the wrong offset) is written into `COLUMN_TRANSACTION_INFO`. After migration completes and the version stamp is updated, the node permanently operates with corrupted transaction info storage, breaking any code path that reads `COLUMN_TRANSACTION_INFO`.

---

### Finding Description

`ChangeMoleculeTableToStruct` (version `20200703124523`, shipped since v0.35.0) is a mandatory, non-background migration that converts several RocksDB column families from Molecule Table encoding to Molecule Struct encoding. It calls four sub-functions in sequence:

1. `migrate_header` — traverses `COLUMN_BLOCK_HEADER`, writes to `COLUMN_BLOCK_HEADER` ✓
2. `migrate_uncles` — traverses `COLUMN_UNCLES`, writes to `COLUMN_UNCLES` ✓
3. `migrate_transaction_info` — **intended** to traverse `COLUMN_TRANSACTION_INFO`, write to `COLUMN_TRANSACTION_INFO` ✗
4. `migrate_epoch_ext` — traverses `COLUMN_EPOCH`, writes to `COLUMN_EPOCH` ✓

The bug is in `migrate_transaction_info`:

```rust
// util/migrate/src/migrations/table_to_struct.rs  lines 77-101
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
        //              ^^^^^^^^^^^^^ BUG: should be COLUMN_TRANSACTION_INFO
        next_key = nk;
        ...
    }
    Ok(())
}
``` [1](#0-0) 

The iterator is opened on `COLUMN_UNCLES` (`"11"`) instead of `COLUMN_TRANSACTION_INFO` (`"5"`). [2](#0-1) [3](#0-2) 

Two concrete consequences follow:

**A. `COLUMN_TRANSACTION_INFO` is never migrated.** All pre-existing transaction info records remain in the old Molecule Table encoding. After the migration version stamp is written, the node will never re-run this migration. Every subsequent read of `COLUMN_TRANSACTION_INFO` using the new Struct decoder will silently produce garbage or panic.

**B. Uncle data is injected into `COLUMN_TRANSACTION_INFO`.** For every uncle entry whose serialized length differs from 52 bytes, the callback fires and writes `uncle_bytes[16..]` — keyed by the uncle's key — into `COLUMN_TRANSACTION_INFO`. This overwrites or pollutes legitimate transaction info slots with structurally invalid data.

The migration is registered as non-background and non-resumable, so it runs once, marks itself complete, and is never retried: [4](#0-3) [5](#0-4) 

After the migration version key is written to `COLUMN_META`, the database is considered up-to-date and the node starts normally, with no indication that transaction info is corrupted. [6](#0-5) 

---

### Impact Explanation

`COLUMN_TRANSACTION_INFO` is read by `get_transaction_info` in the chain store, which is called by:

- The JSON-RPC `get_transaction` endpoint (used by every wallet, explorer, and dApp querying transaction details).
- The light-client proof server (`get_transactions_proof`).
- Transaction verification logic in tests and potentially in the verifier. [7](#0-6) 

After migration, any node that upgraded from a pre-v0.35.0 database will return corrupted or deserialization-failing results for `get_transaction_info`. Depending on how the Molecule Struct decoder handles malformed input, this can cause:

- Silent wrong data returned to RPC callers (incorrect block number / tx index embedded in the response).
- Panics or hard errors inside the node when the decoder rejects the old Table-encoded bytes, crashing the node or causing it to drop the RPC connection.

The migration version stamp is permanently advanced, so there is no self-healing path; the database is irreversibly left in a mixed/corrupted state.

---

### Likelihood Explanation

Any CKB full node that:
1. Was initialized before v0.35.0 (released 2020-10-20), **and**
2. Was upgraded to v0.35.0 or later without a full resync from genesis

will have triggered this migration exactly once and will be permanently affected. The migration path is the standard upgrade path documented for node operators; no special configuration or attacker interaction is required. The entry point is the supported local CLI (`ckb migrate` or automatic migration on node start via `open_or_create_db`). [8](#0-7) [9](#0-8) 

---

### Recommendation

Change line 93 of `util/migrate/src/migrations/table_to_struct.rs` to traverse `COLUMN_TRANSACTION_INFO` instead of `COLUMN_UNCLES`:

```rust
// Before (buggy):
let (_count, nk) =
    db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;

// After (correct):
let (_count, nk) =
    db.traverse(COLUMN_TRANSACTION_INFO, &mut transaction_info_migration, mode, LIMIT)?;
```

Because the migration version stamp has already been written for all affected nodes, a new migration step must also be introduced to re-run the transaction info conversion for nodes that already passed through the broken `ChangeMoleculeTableToStruct` migration.

---

### Proof of Concept

1. Start a CKB node with a database created before v0.35.0 (Molecule Table format in `COLUMN_TRANSACTION_INFO`).
2. Run `ckb migrate` (or start the node, which calls `open_or_create_db` → `Migrate::migrate`).
3. `ChangeMoleculeTableToStruct::migrate` is invoked. It calls `migrate_transaction_info`, which opens an iterator on `COLUMN_UNCLES` (column `"11"`), not `COLUMN_TRANSACTION_INFO` (column `"5"`).
4. Uncle entries whose length ≠ 52 bytes are sliced at offset 16 and written into `COLUMN_TRANSACTION_INFO` under uncle keys.
5. The original transaction info entries in `COLUMN_TRANSACTION_INFO` are untouched (still Table-encoded).
6. Migration version `20200703124523` is written to `COLUMN_META`; the node starts.
7. Call `get_transaction` via RPC for any transaction committed before the migration. The store reads `COLUMN_TRANSACTION_INFO`, attempts to decode the old Table bytes as a Struct, and either returns wrong block/index data or panics. [10](#0-9)

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

**File:** util/migrate/src/migrations/table_to_struct.rs (L132-186)
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

    fn version(&self) -> &str {
        VERSION
    }
}
```

**File:** db-schema/src/lib.rs (L17-18)
```rust
/// Column store transaction extra information
pub const COLUMN_TRANSACTION_INFO: Col = "5";
```

**File:** db-schema/src/lib.rs (L31-32)
```rust
/// <https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0020-ckb-consensus-protocol/0020-ckb-consensus-protocol.md#specification>
pub const COLUMN_UNCLES: Col = "11";
```

**File:** util/migrate/src/migrate.rs (L25-27)
```rust
        migrations.add_migration(Arc::new(DefaultMigration::new(INIT_DB_VERSION)));
        migrations.add_migration(Arc::new(migrations::ChangeMoleculeTableToStruct)); // since v0.35.0
        migrations.add_migration(Arc::new(migrations::CellMigration)); // since v0.37.0
```

**File:** db-migration/src/lib.rs (L83-89)
```rust
                            if let Ok(db) = task.migrate(self.db.clone(), Arc::new(pb)) {
                                db.put_default(MIGRATION_VERSION_KEY, task.version())
                                .map_err(|err| {
                                    internal_error(format!("failed to migrate the database: {err}"))
                                })
                                .unwrap();
                            }
```

**File:** store/src/store.rs (L7-14)
```rust
use ckb_db_schema::{
    COLUMN_BLOCK_BODY, COLUMN_BLOCK_EPOCH, COLUMN_BLOCK_EXT, COLUMN_BLOCK_EXTENSION,
    COLUMN_BLOCK_FILTER, COLUMN_BLOCK_FILTER_HASH, COLUMN_BLOCK_HEADER, COLUMN_BLOCK_PROPOSAL_IDS,
    COLUMN_BLOCK_UNCLE, COLUMN_CELL, COLUMN_CELL_DATA, COLUMN_CELL_DATA_HASH,
    COLUMN_CHAIN_ROOT_MMR, COLUMN_EPOCH, COLUMN_INDEX, COLUMN_META, COLUMN_TRANSACTION_INFO,
    COLUMN_UNCLES, Col, META_CURRENT_EPOCH_KEY, META_LATEST_BUILT_FILTER_DATA_KEY,
    META_TIP_HEADER_KEY,
};
```

**File:** shared/src/shared_builder.rs (L66-121)
```rust
    let migrate = Migrate::new(&config.path, hardforks);

    let read_only_db = migrate.open_read_only_db().map_err(|e| {
        eprintln!("Migration error {e}");
        ExitCode::Failure
    })?;

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
```

**File:** ckb-bin/src/subcommand/migrate.rs (L8-83)
```rust
pub fn migrate(args: MigrateArgs) -> Result<(), ExitCode> {
    let migrate = Migrate::new(&args.config.db.path, args.consensus.hardfork_switch);

    {
        let read_only_db = migrate.open_read_only_db().map_err(|e| {
            eprintln!("Migration error {e}");
            ExitCode::Failure
        })?;

        if let Some(db) = read_only_db {
            // if there are only pending background migrations, they will run automatically
            // so here we check with `include_background` as true
            let db_status = migrate.check(&db, true);
            if matches!(db_status, Ordering::Greater) {
                eprintln!(
                    "The database was created by a higher version CKB executable binary \n\
                     and cannot be opened by the current binary.\n\
                     Please download the latest CKB executable binary."
                );
                return Err(ExitCode::Failure);
            }

            // `include_background` is default to false
            let db_status = migrate.check(&db, args.include_background);
            if args.check {
                if matches!(db_status, Ordering::Less) {
                    // special for bash usage, return 0 means need run migration
                    // if ckb migrate --check; then ckb migrate --force; fi
                    return Ok(());
                } else {
                    return Err(ExitCode::Cli);
                }
            }

            if matches!(db_status, Ordering::Equal) {
                return Ok(());
            }

            if migrate.require_expensive(&db, args.include_background) && !args.force {
                if std::io::stdin().is_terminal() && std::io::stdout().is_terminal() {
                    let input = prompt(
                        "\
                    \n\
                    Doing migration will take quite a long time before CKB could work again.\n\
                    \n\
                    Once the migration started, the data will be no longer compatible with all older versions CKB,\n\
                    so we strongly recommended you to backup the old data before migrating.\n\
                    \n\
                    If the migration failed, try to delete all data and sync from scratch.\n\
                    \nIf you want to migrate the data, please input YES, otherwise, the current process will exit.\n\
                    > ",
                    );
                    if input.trim().to_lowercase() != "yes" {
                        eprintln!("Migration was declined since the user didn't confirm.");
                        return Err(ExitCode::Failure);
                    }
                } else {
                    eprintln!("Run error: use --force to migrate without interactive prompt");
                    return Err(ExitCode::Failure);
                }
            }
        }
    }

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
    Ok(())
```
