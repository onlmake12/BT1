The file confirms the claim exactly. Line 93 calls `db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)` while the closure on line 85 writes to `COLUMN_TRANSACTION_INFO`. `COLUMN_TRANSACTION_INFO` is imported at line 4 but never passed to any `traverse` call in the file.

Audit Report

## Title
`migrate_transaction_info` Traverses `COLUMN_UNCLES` Instead of `COLUMN_TRANSACTION_INFO`, Leaving Transaction Index Uncorrected After `ChangeMoleculeTableToStruct` Migration — (File: `util/migrate/src/migrations/table_to_struct.rs`)

## Summary

In `migrate_transaction_info`, the `db.traverse` call on line 93 passes `COLUMN_UNCLES` as the source column instead of `COLUMN_TRANSACTION_INFO`. The closure correctly targets `COLUMN_TRANSACTION_INFO` as the write destination (line 85), but reads from the wrong column. As a result, the actual transaction-hash-keyed entries in `COLUMN_TRANSACTION_INFO` are never migrated from old Molecule table format, and uncle-derived data is written into `COLUMN_TRANSACTION_INFO` under uncle block hash keys.

## Finding Description

The four sub-functions of `ChangeMoleculeTableToStruct` each reformat one column. Three are correct:

- `migrate_header` (line 40): traverses `COLUMN_BLOCK_HEADER`, writes to `COLUMN_BLOCK_HEADER`
- `migrate_uncles` (line 66): traverses `COLUMN_UNCLES`, writes to `COLUMN_UNCLES`
- `migrate_epoch_ext` (line 120): traverses `COLUMN_EPOCH`, writes to `COLUMN_EPOCH`

`migrate_transaction_info` (lines 77–102) defines a closure that writes to `COLUMN_TRANSACTION_INFO` (line 85), but the `db.traverse` call on line 93 passes `COLUMN_UNCLES` as the source. `COLUMN_TRANSACTION_INFO` is imported at line 4 but is never passed to any `traverse` call anywhere in the file.

Execution flow:
1. Node operator upgrades binary against a DB with version older than `20200703124523`.
2. `ChangeMoleculeTableToStruct::migrate` is called automatically.
3. `migrate_uncles` correctly reformats `COLUMN_UNCLES` (entries now 240 bytes).
4. `migrate_transaction_info` re-reads the already-migrated `COLUMN_UNCLES`; each uncle entry is now exactly 240 bytes, so `value.len() != TRANSACTION_INFO_SIZE (52)` is true for all of them. The closure strips 16 bytes and writes the result into `COLUMN_TRANSACTION_INFO` keyed by uncle block hashes.
5. The real `COLUMN_TRANSACTION_INFO` entries (keyed by tx hash) are never read and remain in old Molecule table format.
6. `migrate` returns `Ok(db)` at line 180 with no error indication.

After migration, `COLUMN_TRANSACTION_INFO` contains: (a) all original tx-hash-keyed entries still in old table format, and (b) uncle-hash-keyed entries containing truncated uncle header data. [1](#0-0) [2](#0-1) [3](#0-2) 

## Impact Explanation

`COLUMN_TRANSACTION_INFO` is the sole index mapping transaction hashes to block locations. After migration, historical transaction lookups via `get_transaction_info`, `get_transaction`, `get_transaction_with_info`, and `transaction_exists` return wrong or absent data because entries remain in old Molecule table format. This is a concrete, incorrect implementation of the CKB state storage mechanism — specifically the transaction index component of the chain store.

**Impact: Medium (2001–10000 points) — Suboptimal/incorrect implementation of CKB state storage mechanism.**

## Likelihood Explanation

The migration triggers automatically when any binary post-v0.35.0 is run against a pre-v0.35.0 database. No attacker action is required; the corruption is self-inflicted by the upgrade process. Likelihood is low in practice (most nodes migrated years ago), but the bug remains present in the production codebase and will silently corrupt any node that encounters this upgrade path, such as operators restoring archival backups or upgrading long-dormant nodes.

## Recommendation

Change line 93 in `util/migrate/src/migrations/table_to_struct.rs`:

```diff
- db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
+ db.traverse(COLUMN_TRANSACTION_INFO, &mut transaction_info_migration, mode, LIMIT)?;
```

Add a regression test that pre-populates both `COLUMN_UNCLES` and `COLUMN_TRANSACTION_INFO` with old-format (table-encoded) entries, runs `ChangeMoleculeTableToStruct::migrate`, and asserts: (a) every `COLUMN_TRANSACTION_INFO` entry is exactly 52 bytes, and (b) no uncle-block-hash-keyed entry appears in `COLUMN_TRANSACTION_INFO`.

## Proof of Concept

Directly visible by static inspection:

1. `COLUMN_TRANSACTION_INFO` is imported at line 4 but never passed to `db.traverse` anywhere in the file. [1](#0-0) 
2. `migrate_uncles` (line 66) and `migrate_transaction_info` (line 93) both call `db.traverse(COLUMN_UNCLES, ...)`. [4](#0-3) 
3. The closure on line 85 writes to `COLUMN_TRANSACTION_INFO`, meaning uncle data is written into the transaction index. [5](#0-4) 
4. `migrate` calls `migrate_transaction_info` at line 159 and returns `Ok(db)` at line 180 with no error. [6](#0-5)

### Citations

**File:** util/migrate/src/migrations/table_to_struct.rs (L3-6)
```rust
use ckb_db_schema::{
    COLUMN_BLOCK_HEADER, COLUMN_EPOCH, COLUMN_META, COLUMN_TRANSACTION_INFO, COLUMN_UNCLES,
    META_CURRENT_EPOCH_KEY,
};
```

**File:** util/migrate/src/migrations/table_to_struct.rs (L66-66)
```rust
            let (_count, nk) = db.traverse(COLUMN_UNCLES, &mut uncles_migration, mode, LIMIT)?;
```

**File:** util/migrate/src/migrations/table_to_struct.rs (L82-88)
```rust
            let mut transaction_info_migration = |key: &[u8], value: &[u8]| -> Result<()> {
                // (1 total size field + 3 fields) * 4 byte per field
                if value.len() != TRANSACTION_INFO_SIZE {
                    wb.put(COLUMN_TRANSACTION_INFO, key, &value[16..])?;
                }
                Ok(())
            };
```

**File:** util/migrate/src/migrations/table_to_struct.rs (L92-94)
```rust
            let (_count, nk) =
                db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
            next_key = nk;
```

**File:** util/migrate/src/migrations/table_to_struct.rs (L157-161)
```rust
        pb.set_message("migrating: transaction info");
        pb.inc(1);
        self.migrate_transaction_info(&db)?;
        pb.set_message("finish: transaction info");
        pb.inc(1);
```
