### Title
Wrong Column Traversal in `migrate_transaction_info` Corrupts Transaction Index During Database Migration - (File: util/migrate/src/migrations/table_to_struct.rs)

### Summary
The `migrate_transaction_info` function in the `ChangeMoleculeTableToStruct` migration traverses `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO`. This is a direct copy-paste analog of the reported wrong-field-assignment class: one column's data is read and written into another column's slot, permanently corrupting the transaction index for any node that runs `ckb migrate`.

### Finding Description
In `util/migrate/src/migrations/table_to_struct.rs`, the `migrate_transaction_info` function is responsible for converting `COLUMN_TRANSACTION_INFO` records from the old molecule table format (52 bytes, with a 16-byte size-field header) to the new struct format (36 bytes, no header). However, the `db.traverse` call on line 93 iterates over `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO`:

```rust
fn migrate_transaction_info(&self, db: &RocksDB) -> Result<()> {
    const TRANSACTION_INFO_SIZE: usize = 52;
    ...
    let (_count, nk) =
        db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
    //             ^^^^^^^^^^^^^ BUG: should be COLUMN_TRANSACTION_INFO
```

Compare with the correct pattern used in `migrate_header` and `migrate_uncles`, where the traversal column matches the write column:

```rust
// migrate_header: traverses COLUMN_BLOCK_HEADER, writes to COLUMN_BLOCK_HEADER ✓
db.traverse(COLUMN_BLOCK_HEADER, &mut header_view_migration, mode, LIMIT)?;

// migrate_uncles: traverses COLUMN_UNCLES, writes to COLUMN_UNCLES ✓
db.traverse(COLUMN_UNCLES, &mut uncles_migration, mode, LIMIT)?;

// migrate_transaction_info: traverses COLUMN_UNCLES, writes to COLUMN_TRANSACTION_INFO ✗
db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
```

The consequence is twofold:

1. **Real transaction info records are never migrated.** The old 52-byte table-format entries remain in `COLUMN_TRANSACTION_INFO` untouched. After migration, `get_transaction_info` calls `packed::TransactionInfoReader::from_slice_should_be_ok` on these stale 52-byte blobs, which are now misinterpreted as the new 36-byte struct layout, producing garbage field values (`block_hash`, `block_number`, `block_epoch`, `index`).

2. **Uncle header data is incorrectly injected into `COLUMN_TRANSACTION_INFO`.** Uncle header entries are 240 bytes. Since `240 != 52` (the `TRANSACTION_INFO_SIZE` check), the closure fires for every uncle, writing `uncle_data[16..]` (224 bytes of uncle header content) into `COLUMN_TRANSACTION_INFO` keyed by uncle hashes. This pollutes the transaction index with non-transaction keys.

### Impact Explanation
After the migration runs:

- `transaction_exists(tx_hash)` (`store/src/store.rs:296`) returns results based on corrupted index data — real transaction hashes may map to malformed `TransactionInfo` structs, and uncle hashes are now spuriously present in the transaction index.
- `get_transaction_info(tx_hash)` returns incorrect `block_hash`, `block_number`, `block_epoch`, and `index` fields for every pre-migration transaction, because the old table-format bytes are parsed as the new struct layout.
- `get_transaction(tx_hash)` (`store/src/store.rs:301`) uses `get_transaction_with_info`, which relies on the corrupted `TransactionInfo` to locate the transaction body in `COLUMN_BLOCK_BODY`. With a wrong `block_hash` or `index`, the lookup either fails or returns the wrong transaction.
- The `calculate_dao_maximum_withdraw` RPC (`rpc/src/module/experiment.rs:247`) calls `snapshot.get_transaction(&out_point.tx_hash())` and uses the returned `deposit_header_hash` directly in DAO interest calculations. A corrupted `block_hash` field causes the wrong deposit header to be used, producing incorrect maximum-withdraw values.
- The `get_block_economic_state` RPC (`rpc/src/module/chain.rs:1902`) calls `block_reward_for_target`, which internally calls `get_block_ext` and `get_cellbase` — paths that depend on correct transaction indexing.

The database corruption is permanent: once `ckb migrate` completes, the node operates on a broken transaction index for all blocks committed before the migration.

### Likelihood Explanation
Any node operator upgrading from a pre-v0.35.0 database to v0.35.0+ and running `ckb migrate` (or `ckb migrate --force`) triggers this path. The `ChangeMoleculeTableToStruct` migration is registered unconditionally in `util/migrate/src/migrate.rs:26` and runs automatically during the migration sequence. No special configuration or attacker interaction is required — the operator simply follows the standard upgrade procedure documented in the CKB release notes.

### Recommendation
**Short term:** In `migrate_transaction_info`, replace `COLUMN_UNCLES` with `COLUMN_TRANSACTION_INFO` in the `db.traverse` call so the function reads from the correct column:

```rust
let (_count, nk) =
    db.traverse(COLUMN_TRANSACTION_INFO, &mut transaction_info_migration, mode, LIMIT)?;
```

**Long term:** Add a post-migration integrity check that verifies each column's record count and byte-size distribution matches expectations. Add unit tests for each `migrate_*` helper that assert the correct source column is traversed and the correct destination column is written.

### Proof of Concept
The bug is statically visible at line 93 of `util/migrate/src/migrations/table_to_struct.rs`:

```
migrate_header:           traverse(COLUMN_BLOCK_HEADER) → put(COLUMN_BLOCK_HEADER)  ✓
migrate_uncles:           traverse(COLUMN_UNCLES)        → put(COLUMN_UNCLES)        ✓
migrate_transaction_info: traverse(COLUMN_UNCLES)        → put(COLUMN_TRANSACTION_INFO) ✗
migrate_epoch_ext:        traverse(COLUMN_EPOCH)         → put(COLUMN_EPOCH)         ✓
```

A node with any uncle blocks stored before migration will have those uncle hashes written into `COLUMN_TRANSACTION_INFO` with 224-byte payloads, while all real transaction info entries remain in the unmigrated 52-byte table format. Any subsequent call to `get_transaction_info` on a pre-migration transaction hash will parse the stale table-format bytes as a struct, returning a `TransactionInfo` with a wrong `block_hash` — the first 32 bytes of the old size-field-prefixed layout rather than the actual block hash. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** util/migrate/src/migrations/table_to_struct.rs (L23-50)
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
```

**File:** util/migrate/src/migrations/table_to_struct.rs (L52-75)
```rust
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

**File:** store/src/store.rs (L293-313)
```rust
    ///
    /// This function is base on transaction index `COLUMN_TRANSACTION_INFO`.
    /// Current release maintains a full index of historical transaction by default, this may be changed in future
    fn transaction_exists(&self, hash: &packed::Byte32) -> bool {
        self.get(COLUMN_TRANSACTION_INFO, hash.as_slice()).is_some()
    }

    /// Get commit transaction and block hash by its hash
    fn get_transaction(&self, hash: &packed::Byte32) -> Option<(TransactionView, packed::Byte32)> {
        self.get_transaction_with_info(hash)
            .map(|(tx, tx_info)| (tx, tx_info.block_hash))
    }

    /// Returns transaction info by transaction hash.
    fn get_transaction_info(&self, hash: &packed::Byte32) -> Option<TransactionInfo> {
        self.get(COLUMN_TRANSACTION_INFO, hash.as_slice())
            .map(|slice| {
                let reader = packed::TransactionInfoReader::from_slice_should_be_ok(slice.as_ref());
                Into::<TransactionInfo>::into(reader)
            })
    }
```

**File:** rpc/src/module/experiment.rs (L246-264)
```rust
            DaoWithdrawingCalculationKind::WithdrawingHeaderHash(withdrawing_header_hash) => {
                let (tx, deposit_header_hash) = snapshot
                    .get_transaction(&out_point.tx_hash())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;
                let output = tx
                    .outputs()
                    .get(out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;
                let output_data = tx
                    .outputs_data()
                    .get(out_point.index().into())
                    .ok_or_else(|| RPCError::invalid_params("invalid out_point"))?;

                match calculator.calculate_maximum_withdraw(
                    &output,
                    core::Capacity::bytes(output_data.len()).expect("should not overflow"),
                    &deposit_header_hash,
                    &withdrawing_header_hash.into(),
                ) {
```

**File:** util/migrate/src/migrate.rs (L25-27)
```rust
        migrations.add_migration(Arc::new(DefaultMigration::new(INIT_DB_VERSION)));
        migrations.add_migration(Arc::new(migrations::ChangeMoleculeTableToStruct)); // since v0.35.0
        migrations.add_migration(Arc::new(migrations::CellMigration)); // since v0.37.0
```
