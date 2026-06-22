### Title
Wrong Column Traversed in `migrate_transaction_info` Leaves `COLUMN_TRANSACTION_INFO` in Old Molecule Table Format, Causing Node Panic on Transaction Lookup — (File: `util/migrate/src/migrations/table_to_struct.rs`)

---

### Summary

The `ChangeMoleculeTableToStruct` migration's `migrate_transaction_info` function traverses `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO` due to a copy-paste error. This means all pre-migration `TransactionInfo` records are never converted from the old molecule table format (68 bytes) to the new struct format (52 bytes). After the migration completes, any read of `COLUMN_TRANSACTION_INFO` via `from_slice_should_be_ok` panics because the stored slice length does not match the expected struct size, crashing the node on any committed-transaction RPC query.

---

### Finding Description

The `ChangeMoleculeTableToStruct` migration (version `20200703124523`, registered as the first real migration in `util/migrate/src/migrate.rs`) is responsible for converting three molecule tables to fixed-size structs: `HeaderView`, `EpochExt`, and `TransactionInfo`. Every other sub-function correctly reads and rewrites its own column:

- `migrate_header`: traverses `COLUMN_BLOCK_HEADER`, writes `COLUMN_BLOCK_HEADER` ✓
- `migrate_uncles`: traverses `COLUMN_UNCLES`, writes `COLUMN_UNCLES` ✓
- `migrate_epoch_ext`: traverses `COLUMN_EPOCH`, writes `COLUMN_EPOCH` ✓

But `migrate_transaction_info` contains a copy-paste error at line 93:

```rust
// util/migrate/src/migrations/table_to_struct.rs  lines 77-101
fn migrate_transaction_info(&self, db: &RocksDB) -> Result<()> {
    const TRANSACTION_INFO_SIZE: usize = 52;
    ...
    let mut transaction_info_migration = |key: &[u8], value: &[u8]| -> Result<()> {
        if value.len() != TRANSACTION_INFO_SIZE {
            wb.put(COLUMN_TRANSACTION_INFO, key, &value[16..])?;  // writes to correct column
        }
        Ok(())
    };
    ...
    let (_count, nk) =
        db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;  // BUG: reads wrong column
    ...
}
```

Two concrete effects result:

1. **`COLUMN_TRANSACTION_INFO` is never migrated.** Every `TransactionInfo` record written before this migration is in the old molecule table format (68 bytes: 16-byte table header + 52-byte payload). The migration closure is never invoked on them, so they remain at 68 bytes.

2. **Uncle header data is spuriously written into `COLUMN_TRANSACTION_INFO`.** The closure iterates over `COLUMN_UNCLES` entries. For any uncle whose serialized `HeaderView` is not exactly 52 bytes (virtually all of them), it writes `uncle_value[16..]` into `COLUMN_TRANSACTION_INFO` keyed by the uncle hash. This pollutes the transaction index with garbage entries under uncle-hash keys.

After the migration, the read path in `store/src/store.rs` calls:

```rust
// store/src/store.rs  lines 307-313
fn get_transaction_info(&self, hash: &packed::Byte32) -> Option<TransactionInfo> {
    self.get(COLUMN_TRANSACTION_INFO, hash.as_slice())
        .map(|slice| {
            let reader = packed::TransactionInfoReader::from_slice_should_be_ok(slice.as_ref());
            Into::<TransactionInfo>::into(reader)
        })
}
```

`TransactionInfoReader::verify` enforces an exact size check:

```rust
// util/gen-types/src/generated/extensions.rs  lines 5642-5648
fn verify(slice: &[u8], _compatible: bool) -> molecule::error::VerificationResult<()> {
    let slice_len = slice.len();
    if slice_len != Self::TOTAL_SIZE {          // TOTAL_SIZE = 52
        return ve!(Self, TotalSizeNotMatch, Self::TOTAL_SIZE, slice_len);
    }
    Ok(())
}
```

`from_slice_should_be_ok` panics on a verification error. Any pre-migration `TransactionInfo` record is 68 bytes, so every call to `get_transaction_info` for a committed transaction panics, crashing the node process.

---

### Impact Explanation

**Impact: High**

Any node that ran the `ChangeMoleculeTableToStruct` migration on a pre-v0.35.0 database and then queries a committed transaction via RPC will panic. The affected RPC entry points include:

- `get_transaction` (verbosity 1 and 2) — calls `get_transaction_info` / `get_transaction_with_info`
- `get_transaction_proof` — calls `get_transaction_info` via `get_tx_indices`
- Any internal chain logic that calls `transaction_exists` or `get_transaction_with_info`

A panic in any of these paths terminates the node process. Because the corruption is persistent in the database, the node will crash on every restart as soon as any committed-transaction query is issued. Recovery requires a full re-sync or manual database repair. No funds are directly at risk, but the node becomes permanently unavailable for transaction queries.

---

### Likelihood Explanation

**Likelihood: Low**

The migration targets databases older than v0.35.0 (released 2020). Most production nodes have long since passed this version. However:

- The migration is still present and active in the migration chain (`util/migrate/src/migrate.rs` line 26).
- Any operator restoring from a pre-v0.35.0 snapshot, importing an archival backup, or bootstrapping from a very old chain export will trigger this migration and silently corrupt `COLUMN_TRANSACTION_INFO`.
- The corruption is silent — the migration reports success and updates the version key — so operators have no indication that transaction info was not migrated.

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

Additionally, consider adding a post-migration integrity check that verifies all entries in `COLUMN_TRANSACTION_INFO` are exactly 52 bytes, and purge any spurious uncle-hash-keyed entries that were incorrectly written by the buggy migration.

---

### Proof of Concept

1. Start a CKB node with a database at version `< 20200703124523` (pre-v0.35.0).
2. Upgrade the binary to any version that includes `ChangeMoleculeTableToStruct`.
3. The migration runs. `migrate_transaction_info` traverses `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO`. All `TransactionInfo` records remain in the 68-byte table format. Some uncle-hash keys are written into `COLUMN_TRANSACTION_INFO` with uncle body data.
4. The migration version key is updated to `20200703124523`; no error is reported.
5. Issue any RPC call that reads a committed transaction, e.g., `get_transaction <any_committed_tx_hash>`.
6. `get_transaction_info` reads the 68-byte record from `COLUMN_TRANSACTION_INFO` and calls `from_slice_should_be_ok`.
7. `TransactionInfoReader::verify` returns `TotalSizeNotMatch(52, 68)`.
8. `from_slice_should_be_ok` panics → node process terminates.
9. On every subsequent restart, the same panic recurs for any committed-transaction query.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** util/gen-types/src/generated/extensions.rs (L5642-5648)
```rust
    fn verify(slice: &[u8], _compatible: bool) -> molecule::error::VerificationResult<()> {
        use molecule::verification_error as ve;
        let slice_len = slice.len();
        if slice_len != Self::TOTAL_SIZE {
            return ve!(Self, TotalSizeNotMatch, Self::TOTAL_SIZE, slice_len);
        }
        Ok(())
```

**File:** util/migrate/src/migrate.rs (L25-27)
```rust
        migrations.add_migration(Arc::new(DefaultMigration::new(INIT_DB_VERSION)));
        migrations.add_migration(Arc::new(migrations::ChangeMoleculeTableToStruct)); // since v0.35.0
        migrations.add_migration(Arc::new(migrations::CellMigration)); // since v0.37.0
```
