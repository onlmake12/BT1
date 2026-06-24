Audit Report

## Title
Wrong Column Traversed in `migrate_transaction_info` Leaves `COLUMN_TRANSACTION_INFO` Unmigrated, Causing Node Panic on Transaction Lookup — (File: `util/migrate/src/migrations/table_to_struct.rs`)

## Summary

The `ChangeMoleculeTableToStruct` migration's `migrate_transaction_info` function traverses `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO` at line 93, confirmed in the actual source. All pre-migration `TransactionInfo` records remain in the old 68-byte molecule table format. After migration, any call to `get_transaction_info` invokes `from_slice_should_be_ok` on a 68-byte slice where 52 bytes are expected, causing a panic that terminates the node process.

## Finding Description

The bug is confirmed at `util/migrate/src/migrations/table_to_struct.rs` line 93:

```rust
let (_count, nk) =
    db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
```

The closure `transaction_info_migration` correctly writes to `COLUMN_TRANSACTION_INFO` (line 85), but it is never invoked on any `TransactionInfo` record because the traversal reads from `COLUMN_UNCLES`. Every other sub-migration correctly reads its own column (`migrate_header` → `COLUMN_BLOCK_HEADER`, `migrate_uncles` → `COLUMN_UNCLES`, `migrate_epoch_ext` → `COLUMN_EPOCH`).

Two concrete effects:
1. All `TransactionInfo` records in `COLUMN_TRANSACTION_INFO` remain at 68 bytes (16-byte table header + 52-byte payload), never stripped to 52 bytes.
2. Uncle header data whose serialized length ≠ 52 bytes is spuriously written into `COLUMN_TRANSACTION_INFO` under uncle-hash keys.

The read path in `store/src/store.rs` lines 307–313 calls `packed::TransactionInfoReader::from_slice_should_be_ok(slice.as_ref())`. The molecule-generated `verify` enforces `slice.len() == TOTAL_SIZE (52)`; `from_slice_should_be_ok` panics on any verification failure. A 68-byte slice always fails this check, so every call to `get_transaction_info` for a pre-migration committed transaction panics, terminating the node.

The migration is still registered and active in `util/migrate/src/migrate.rs` line 26, and reports success with no error after corrupting the database.

## Impact Explanation

**High — Crashes a CKB node.**

Any node that ran `ChangeMoleculeTableToStruct` on a pre-v0.35.0 database has a permanently corrupted `COLUMN_TRANSACTION_INFO`. An unprivileged external user can trigger the panic by issuing any RPC call that reads a committed transaction (`get_transaction`, `get_transaction_proof`, etc.). The panic terminates the node process. Because the corruption is persistent in the database, the node crashes on every restart as soon as any committed-transaction query is issued. Recovery requires a full re-sync or manual database repair.

## Likelihood Explanation

**Low.** The migration targets databases older than v0.35.0 (released 2020). Most production nodes have long since passed this version. However, the migration remains active in the chain; any operator restoring from a pre-v0.35.0 snapshot, importing an archival backup, or bootstrapping from a very old chain export will trigger it. The corruption is silent — the migration reports success and updates the version key — so operators have no indication that transaction info was not migrated. Once triggered, the crash is deterministic and repeatable.

## Recommendation

Change line 93 of `util/migrate/src/migrations/table_to_struct.rs` from:

```rust
db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
```

to:

```rust
db.traverse(COLUMN_TRANSACTION_INFO, &mut transaction_info_migration, mode, LIMIT)?;
```

Additionally, add a post-migration integrity check that verifies all entries in `COLUMN_TRANSACTION_INFO` are exactly 52 bytes, and purge any spurious uncle-hash-keyed entries written by the buggy migration.

## Proof of Concept

1. Start a CKB node with a database at version `< 20200703124523` (pre-v0.35.0) containing committed transactions.
2. Upgrade the binary to any version that includes `ChangeMoleculeTableToStruct`.
3. The migration runs. `migrate_transaction_info` traverses `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO`. All `TransactionInfo` records remain in the 68-byte table format. Some uncle-hash keys are written into `COLUMN_TRANSACTION_INFO` with uncle body data.
4. The migration version key is updated to `20200703124523`; no error is reported.
5. Issue any RPC call that reads a committed transaction, e.g., `get_transaction <any_committed_tx_hash>`.
6. `get_transaction_info` reads the 68-byte record from `COLUMN_TRANSACTION_INFO` and calls `from_slice_should_be_ok`.
7. `TransactionInfoReader::verify` returns `TotalSizeNotMatch(52, 68)`.
8. `from_slice_should_be_ok` panics → node process terminates.
9. On every subsequent restart, the same panic recurs for any committed-transaction query.