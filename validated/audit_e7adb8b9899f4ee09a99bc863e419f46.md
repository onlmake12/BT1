Audit Report

## Title
Wrong Column Traversed in `migrate_transaction_info` Leaves `COLUMN_TRANSACTION_INFO` in Old Molecule Table Format, Causing Node Panic on Transaction Lookup — (File: `util/migrate/src/migrations/table_to_struct.rs`)

## Summary
The `ChangeMoleculeTableToStruct` migration's `migrate_transaction_info` function traverses `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO` at line 93, due to a copy-paste error. As a result, pre-migration `TransactionInfo` records remain in the old 68-byte molecule table format rather than being converted to the expected 52-byte struct format. Any subsequent call to `get_transaction_info` invokes `from_slice_should_be_ok` on a 68-byte slice, which panics on the size mismatch, crashing the node process.

## Finding Description
In `util/migrate/src/migrations/table_to_struct.rs`, the `migrate_transaction_info` function defines a closure that correctly writes to `COLUMN_TRANSACTION_INFO`, but passes `COLUMN_UNCLES` to `db.traverse` at line 93:

```rust
let (_count, nk) =
    db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
``` [1](#0-0) 

Every other sub-migration correctly passes its own column to `db.traverse` — `migrate_header` passes `COLUMN_BLOCK_HEADER`, `migrate_uncles` passes `COLUMN_UNCLES`, and `migrate_epoch_ext` passes `COLUMN_EPOCH`. Only `migrate_transaction_info` is wrong. [2](#0-1) [3](#0-2) [4](#0-3) 

Two concrete effects follow:

1. **`COLUMN_TRANSACTION_INFO` is never migrated.** All pre-migration `TransactionInfo` records remain at 68 bytes (16-byte table header + 52-byte payload).
2. **Uncle header data is spuriously written into `COLUMN_TRANSACTION_INFO`.** The closure iterates `COLUMN_UNCLES` entries (uncle `HeaderView`, 240 bytes). Since `240 != 52`, the condition `value.len() != TRANSACTION_INFO_SIZE` is true for every uncle, so `uncle_value[16..]` (224 bytes) is written into `COLUMN_TRANSACTION_INFO` under uncle-hash keys, polluting the transaction index. [5](#0-4) 

After migration, `get_transaction_info` in `store/src/store.rs` reads from `COLUMN_TRANSACTION_INFO` and calls `from_slice_should_be_ok` on the result: [6](#0-5) 

`from_slice_should_be_ok` is implemented to panic on any verification error: [7](#0-6) 

`TransactionInfoReader::verify` enforces an exact size check (`TOTAL_SIZE = 52`). A 68-byte record fails this check, causing `from_slice_should_be_ok` to panic and terminate the node process. The migration is registered and active in the migration chain: [8](#0-7) 

## Impact Explanation
**High — Crashes a CKB node.** Any node that ran `ChangeMoleculeTableToStruct` on a pre-v0.35.0 database will have a permanently corrupted `COLUMN_TRANSACTION_INFO`. Every call to `get_transaction_info` for a committed transaction panics, crashing the node. Because the corruption is persistent in the database, the node crashes on every restart as soon as any committed-transaction query is issued (via `get_transaction`, `get_transaction_proof`, `transaction_exists`, or any internal chain logic calling `get_transaction_with_info`). Recovery requires a full re-sync or manual database repair. This matches the allowed impact: **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation
**Low.** The migration targets databases older than v0.35.0 (released 2020). Most production nodes have long since passed this version. However, the migration remains active in the chain. Any operator restoring from a pre-v0.35.0 snapshot, importing an archival backup, or bootstrapping from a very old chain export will trigger this migration and silently corrupt `COLUMN_TRANSACTION_INFO`. The corruption is silent — the migration reports success and updates the version key — so operators have no indication that transaction info was not migrated.

## Recommendation
Change line 93 of `util/migrate/src/migrations/table_to_struct.rs` from:

```rust
db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
```

to:

```rust
db.traverse(COLUMN_TRANSACTION_INFO, &mut transaction_info_migration, mode, LIMIT)?;
```

Additionally, consider adding a post-migration integrity check that verifies all entries in `COLUMN_TRANSACTION_INFO` are exactly 52 bytes, and purge any spurious uncle-hash-keyed entries that were incorrectly written by the buggy migration. [9](#0-8) 

## Proof of Concept
1. Start a CKB node with a database at version `< 20200703124523` (pre-v0.35.0).
2. Upgrade the binary to any version that includes `ChangeMoleculeTableToStruct`.
3. The migration runs. `migrate_transaction_info` traverses `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO`. All `TransactionInfo` records remain in the 68-byte table format. Uncle-hash keys are written into `COLUMN_TRANSACTION_INFO` with 224-byte uncle body slices.
4. The migration version key is updated to `20200703124523`; no error is reported.
5. Issue any RPC call that reads a committed transaction, e.g., `get_transaction <any_committed_tx_hash>`.
6. `get_transaction_info` reads the 68-byte record from `COLUMN_TRANSACTION_INFO` and calls `from_slice_should_be_ok`.
7. `TransactionInfoReader::verify` returns `TotalSizeNotMatch(52, 68)`.
8. `from_slice_should_be_ok` panics with `"failed to convert from slice: reason: ...; data: 0x..."` → node process terminates.
9. On every subsequent restart, the same panic recurs for any committed-transaction query.

### Citations

**File:** util/migrate/src/migrations/table_to_struct.rs (L39-40)
```rust
            let (_count, nk) =
                db.traverse(COLUMN_BLOCK_HEADER, &mut header_view_migration, mode, LIMIT)?;
```

**File:** util/migrate/src/migrations/table_to_struct.rs (L66-66)
```rust
            let (_count, nk) = db.traverse(COLUMN_UNCLES, &mut uncles_migration, mode, LIMIT)?;
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

**File:** util/migrate/src/migrations/table_to_struct.rs (L120-120)
```rust
            let (_count, nk) = db.traverse(COLUMN_EPOCH, &mut epoch_ext_migration, mode, LIMIT)?;
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

**File:** util/gen-types/src/prelude.rs (L45-53)
```rust
    fn from_slice_should_be_ok(slice: &'r [u8]) -> Self {
        match Self::from_slice(slice) {
            Ok(ret) => ret,
            Err(err) => panic!(
                "failed to convert from slice: reason: {}; data: 0x{}.",
                err,
                hex_string(slice)
            ),
        }
```

**File:** util/migrate/src/migrate.rs (L25-27)
```rust
        migrations.add_migration(Arc::new(DefaultMigration::new(INIT_DB_VERSION)));
        migrations.add_migration(Arc::new(migrations::ChangeMoleculeTableToStruct)); // since v0.35.0
        migrations.add_migration(Arc::new(migrations::CellMigration)); // since v0.37.0
```
