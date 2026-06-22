### Title
Wrong Column Family in `migrate_transaction_info` Corrupts Transaction Info Storage — (`File: util/migrate/src/migrations/table_to_struct.rs`)

---

### Summary

The `migrate_transaction_info` function inside the `ChangeMoleculeTableToStruct` database migration traverses `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO`. This is a direct analog to the Scroll bug: a hardcoded storage-location identifier is wrong, causing the function to read from the wrong storage slot. The result is that uncle-block data is written into the transaction-info column (corrupting it), while the actual transaction-info records are never migrated.

---

### Finding Description

In `util/migrate/src/migrations/table_to_struct.rs`, the `migrate_transaction_info` function is responsible for converting old molecule-table-encoded `TransactionInfo` records to the new struct encoding. The function correctly targets `COLUMN_TRANSACTION_INFO` for writes, but the `db.traverse(...)` call that reads the source data passes `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO`:

```rust
fn migrate_transaction_info(&self, db: &RocksDB) -> Result<()> {
    const TRANSACTION_INFO_SIZE: usize = 52;
    ...
    let (_count, nk) =
        db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
    //             ^^^^^^^^^^^^^ should be COLUMN_TRANSACTION_INFO
``` [1](#0-0) 

The correct column constants are defined in `db-schema/src/lib.rs`:

- `COLUMN_TRANSACTION_INFO = "5"` — stores transaction info keyed by tx hash
- `COLUMN_UNCLES = "11"` — stores uncle block `HeaderView` records (240 bytes each) keyed by uncle hash [2](#0-1) 

The `TransactionInfo` struct (from `extensions.mol`) has three fixed-size fields totalling 52 bytes:

```
struct TransactionInfo {
    block_number:   Uint64,   // 8 bytes
    block_epoch:    Uint64,   // 8 bytes
    key:            TransactionKey,  // 36 bytes (Byte32 + BeUint32)
}
``` [3](#0-2) 

The migration's guard condition `if value.len() != TRANSACTION_INFO_SIZE` (52) is always true for uncle `HeaderView` records (240 bytes), so every uncle entry passes the guard and has 16 bytes stripped from the front, with the remaining 224 bytes written into `COLUMN_TRANSACTION_INFO` keyed by uncle hash. Meanwhile, the actual `COLUMN_TRANSACTION_INFO` records are never read or rewritten.

The migration is registered and runs as part of the standard node upgrade path: [4](#0-3) 

---

### Impact Explanation

After `ChangeMoleculeTableToStruct` runs on a node that held pre-v0.35.0 data:

1. **`COLUMN_TRANSACTION_INFO` is overwritten** with truncated uncle `HeaderView` bytes, keyed by uncle hashes — not transaction hashes. Any subsequent `get_transaction_info(tx_hash)` lookup returns `None` or garbage.
2. **Old table-encoded `TransactionInfo` records are never migrated**, so they remain in the old format and are unreadable by post-migration code.
3. **Transaction status RPC calls** (`get_transaction`) silently return incorrect results.
4. **The tx-pool's duplicate-detection** relies on `TransactionInfo` to check whether a transaction is already confirmed. With corrupted data, the pool may re-accept already-confirmed transactions, breaking mempool integrity.
5. **Block assembly** (`get_block_template`) may include already-confirmed transactions, producing invalid block templates.

---

### Likelihood Explanation

Any CKB full node operator who ran the node on a database created before v0.35.0 (released 2020-07-03) and then upgraded would have triggered this migration. The migration runs automatically on node startup when the stored DB version is below `20200703124523`. The code path requires no attacker involvement — it is triggered by the standard node upgrade procedure performed by the node operator (a "supported local CLI/RPC user" in scope).

---

### Recommendation

Change line 93 of `util/migrate/src/migrations/table_to_struct.rs` to traverse `COLUMN_TRANSACTION_INFO` instead of `COLUMN_UNCLES`:

```rust
let (_count, nk) =
    db.traverse(COLUMN_TRANSACTION_INFO, &mut transaction_info_migration, mode, LIMIT)?;
```

Additionally, add a regression test that verifies each migration sub-function reads from and writes to the correct column family. Consider adding a compile-time or runtime assertion that the source column passed to `db.traverse` matches the column written in the closure.

---

### Proof of Concept

**Root cause (exact lines):** [5](#0-4) 

The closure writes to `COLUMN_TRANSACTION_INFO`: [6](#0-5) 

But the traversal reads from `COLUMN_UNCLES` (line 93), not `COLUMN_TRANSACTION_INFO`.

**Contrast with the correct sibling functions** — `migrate_header` traverses `COLUMN_BLOCK_HEADER` and writes to `COLUMN_BLOCK_HEADER`; `migrate_uncles` traverses `COLUMN_UNCLES` and writes to `COLUMN_UNCLES`; `migrate_epoch_ext` traverses `COLUMN_EPOCH` and writes to `COLUMN_EPOCH`. Only `migrate_transaction_info` has a mismatch between the traversal source and the intended target column. [7](#0-6)

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

**File:** util/migrate/src/migrations/table_to_struct.rs (L77-100)
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
```

**File:** db-schema/src/lib.rs (L17-32)
```rust
/// Column store transaction extra information
pub const COLUMN_TRANSACTION_INFO: Col = "5";
/// Column store block extra information
pub const COLUMN_BLOCK_EXT: Col = "6";
/// Column store block's proposal ids
pub const COLUMN_BLOCK_PROPOSAL_IDS: Col = "7";
/// Column store indicates track block epoch
pub const COLUMN_BLOCK_EPOCH: Col = "8";
/// Column store indicates track block epoch
pub const COLUMN_EPOCH: Col = "9";
/// Column store cell
pub const COLUMN_CELL: Col = "10";
/// Column store main chain consensus include uncles
///
/// <https://github.com/nervosnetwork/rfcs/blob/master/rfcs/0020-ckb-consensus-protocol/0020-ckb-consensus-protocol.md#specification>
pub const COLUMN_UNCLES: Col = "11";
```

**File:** util/gen-types/schemas/extensions.mol (L105-109)
```text
struct TransactionInfo {
    block_number:   Uint64,
    block_epoch:    Uint64,
    key:            TransactionKey,
}
```

**File:** util/migrate/src/migrate.rs (L26-26)
```rust
        migrations.add_migration(Arc::new(migrations::ChangeMoleculeTableToStruct)); // since v0.35.0
```
