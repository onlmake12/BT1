The code confirms the bug exactly as described. At line 93 of `util/migrate/src/migrations/table_to_struct.rs`, `COLUMN_UNCLES` is passed to `db.traverse` instead of `COLUMN_TRANSACTION_INFO`. The closure correctly targets `COLUMN_TRANSACTION_INFO` for writes, but iterates the wrong source column. All other sub-migrations pass their own column. `get_transaction_info` in `store/src/store.rs` calls `from_slice_should_be_ok` which panics on size mismatch. The migration is active in the chain at `util/migrate/src/migrate.rs` line 26.

Audit Report

## Title
Wrong Column Traversed in `migrate_transaction_info` Leaves `COLUMN_TRANSACTION_INFO` in Old Molecule Table Format, Causing Node Panic on Transaction Lookup — (File: `util/migrate/src/migrations/table_to_struct.rs`)

## Summary
`ChangeMoleculeTableToStruct::migrate_transaction_info` traverses `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO` at line 93 due to a copy-paste error. Pre-migration `TransactionInfo` records are never converted from the 68-byte table format to the 52-byte struct format. Any subsequent call to `get_transaction_info` invokes `from_slice_should_be_ok` on a 68-byte slice, which panics and terminates the node process.

## Finding Description
In `util/migrate/src/migrations/table_to_struct.rs`, `migrate_transaction_info` defines a closure that correctly writes to `COLUMN_TRANSACTION_INFO`, but at line 93 passes `COLUMN_UNCLES` to `db.traverse`:

```rust
let (_count, nk) =
    db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
```

Every other sub-migration passes its own column: `migrate_header` passes `COLUMN_BLOCK_HEADER` (line 40), `migrate_uncles` passes `COLUMN_UNCLES` (line 66), `migrate_epoch_ext` passes `COLUMN_EPOCH` (line 120). Only `migrate_transaction_info` is wrong.

Two concrete effects follow:

1. **`COLUMN_TRANSACTION_INFO` is never migrated.** All pre-migration `TransactionInfo` records remain at 68 bytes (16-byte table header + 52-byte payload).
2. **Uncle data is spuriously written into `COLUMN_TRANSACTION_INFO`.** The closure iterates `COLUMN_UNCLES` entries (uncle `HeaderView`, 240 bytes). Since `240 != 52`, the condition `value.len() != TRANSACTION_INFO_SIZE` is true for every uncle entry, so `value[16..]` (224 bytes) is written into `COLUMN_TRANSACTION_INFO` under uncle-hash keys.

After migration, `get_transaction_info` in `store/src/store.rs` lines 307–313 reads from `COLUMN_TRANSACTION_INFO` and calls `from_slice_should_be_ok` on the result. `TransactionInfoReader::verify` enforces an exact size check (`TOTAL_SIZE = 52`). A 68-byte record fails this check, causing `from_slice_should_be_ok` to panic and terminate the node process. The migration is registered and active in the migration chain at `util/migrate/src/migrate.rs` line 26.

## Impact Explanation
**High (10001–15000 points) — Crashes a CKB node.** Any node that ran `ChangeMoleculeTableToStruct` on a pre-v0.35.0 database will have a permanently corrupted `COLUMN_TRANSACTION_INFO`. Every call to `get_transaction_info` for a committed transaction panics, crashing the node. Because the corruption is persistent in the database, the node crashes on every restart as soon as any committed-transaction query is issued via `get_transaction`, `get_transaction_proof`, `transaction_exists`, or any internal chain logic calling `get_transaction_with_info`. Recovery requires a full re-sync or manual database repair.

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

Additionally, add a post-migration integrity check that verifies all entries in `COLUMN_TRANSACTION_INFO` are exactly 52 bytes, and purge any spurious uncle-hash-keyed entries that were incorrectly written by the buggy migration.

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