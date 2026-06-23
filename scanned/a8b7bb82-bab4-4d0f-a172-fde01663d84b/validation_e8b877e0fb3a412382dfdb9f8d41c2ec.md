### Title
Silent Migration Skip When `open_bulk_load_db` Returns `None` Causes Node to Start with Outdated Database Schema — (File: `shared/src/shared_builder.rs`)

---

### Summary

In `open_or_create_db`, when the database requires fast (non-expensive) migration, the code opens a bulk-load-optimized DB handle, runs migrations on it, then reopens the DB normally. However, the migration execution is guarded by `if let Some(db) = bulk_load_db_db { ... }`. If `open_bulk_load_db()` returns `None`, the migration is **silently skipped** and the node starts with an unmigrated, schema-inconsistent database.

---

### Finding Description

In `shared/src/shared_builder.rs`, the `open_or_create_db` function handles three cases when the DB version is behind the binary's expected version (`Ordering::Less`):

1. Expensive migration required → print warning, return `Err`.
2. All pending migrations can run in background → launch async migration, return `Ok(db)`.
3. **Fast (non-expensive) migrations** → open bulk-load DB, run migrations, reopen normal DB.

The third path is:

```rust
// shared/src/shared_builder.rs lines 106-120
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

When `open_bulk_load_db()` returns `Ok(None)` — which is a valid, non-error return — the `if let Some(db)` guard causes the entire migration to be **silently skipped**. The function then returns `Ok(RocksDB::open(config, COLUMNS))`, opening the original, unmigrated database as if migration had succeeded. There is no error, no log warning, and no abort. The node proceeds to full operation on a schema-inconsistent database.

This is directly analogous to the reported pattern: a critical initialization step (migration) is assumed to have been performed, but the code path that performs it is never actually reached, causing all downstream operations that depend on the new schema to behave incorrectly or fail.

---

### Impact Explanation

The CKB database schema evolves across versions. Migrations rewrite stored data (block bodies, transaction indices, epoch data, etc.) into new formats that the new binary expects to read. If a pending fast migration is skipped:

- **Chain store reads return malformed or missing data**: `ChainDB` methods such as `get_block_ext`, `get_tip_header`, `get_current_epoch_ext` read columns whose layout the migration was supposed to update. Stale layout causes incorrect deserialization.
- **Consensus validation is broken**: `TransactionScriptsVerifier` and block verification depend on correctly stored cell data. Incorrect reads can cause valid blocks to be rejected or invalid blocks to be accepted.
- **Snapshot and proposal table initialization fails silently**: `init_snapshot` and `init_proposal_table` in `SharedBuilder::build` read from the same store, propagating the corrupted state into the live snapshot used by all subsystems (tx-pool, relay, sync).
- **No crash at startup**: Because the error is silent, the node appears healthy while operating on corrupt state, making the condition persistent and hard to detect.

---

### Likelihood Explanation

Any node operator who upgrades the CKB binary and runs `ckb run` is in scope. The condition triggers when:

1. The existing DB version is behind the binary's expected version (normal after any upgrade).
2. The pending migrations are all non-expensive (common for minor schema bumps).
3. `open_bulk_load_db()` returns `Ok(None)` rather than `Ok(Some(db))`.

Condition 3 depends on the implementation of `open_bulk_load_db`. Since it returns `Option<RocksDB>` rather than `RocksDB`, `None` is a designed return value, not an error. Any filesystem condition, permission issue, or internal heuristic that causes it to return `None` silently bypasses all migration logic. A node operator running a standard upgrade workflow is the entry point; no attacker privilege is required.

---

### Recommendation

Replace the silent `if let Some` guard with an explicit error when `open_bulk_load_db` returns `None` in a context where migration is required:

```rust
let bulk_load_db_db = migrate.open_bulk_load_db().map_err(|e| {
    eprintln!("Migration error {e}");
    ExitCode::Failure
})?;

match bulk_load_db_db {
    Some(db) => {
        migrate.migrate(db, false).map_err(|err| {
            eprintln!("Run error: {err:?}");
            ExitCode::Failure
        })?;
    }
    None => {
        eprintln!(
            "Migration error: could not open database in bulk-load mode. \
             Migration cannot proceed. Please run `ckb migrate` manually."
        );
        return Err(ExitCode::Failure);
    }
}

Ok(RocksDB::open(config, COLUMNS))
```

This ensures that a missing bulk-load DB handle is treated as a hard failure rather than a silent no-op, preventing the node from starting on an unmigrated schema.

---

### Proof of Concept

1. Run a CKB node with binary version N, accumulate some chain data (DB version = N).
2. Upgrade to binary version N+1, which adds one or more fast (non-expensive) migrations.
3. Arrange for `open_bulk_load_db()` to return `Ok(None)` (e.g., via filesystem permission on the DB path that allows read but not the specific flags used by bulk-load open).
4. Run `ckb run`.
5. Observe: no error is printed, the node starts normally.
6. Observe: `MIGRATION_VERSION_KEY` in the DB still holds version N, not N+1.
7. Observe: any chain operation that reads data in the N+1 schema format returns incorrect results, causing downstream consensus or validation failures reachable by any peer submitting blocks or transactions.

**Root cause line**: [1](#0-0) 

**Surrounding context (fast migration branch)**: [2](#0-1) 

**Migration version write (only reached inside the `Some` branch)**: [3](#0-2) 

**`init_db_version` (called only for brand-new DBs, not for this upgrade path)**: [4](#0-3)

### Citations

**File:** shared/src/shared_builder.rs (L106-121)
```rust
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

**File:** db-migration/src/lib.rs (L236-239)
```rust
            db = m.migrate(db, Arc::new(pb))?;
            db.put_default(MIGRATION_VERSION_KEY, m.version())
                .map_err(|err| internal_error(format!("failed to migrate the database: {err}")))?;
        }
```

**File:** db-migration/src/lib.rs (L288-298)
```rust
    pub fn init_db_version(&self, db: &RocksDB) -> Result<(), Error> {
        let db_version = self.get_migration_version(db)?;
        if db_version.is_none()
            && let Some(m) = self.migrations.values().last()
        {
            info!("Init database version {}", m.version());
            db.put_default(MIGRATION_VERSION_KEY, m.version())
                .map_err(|err| internal_error(format!("failed to migrate the database: {err}")))?;
        }
        Ok(())
    }
```
