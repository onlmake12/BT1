The code is confirmed. Line 93 of `util/migrate/src/migrations/table_to_struct.rs` clearly shows `COLUMN_UNCLES` used as the traversal source inside `migrate_transaction_info`, while the write destination on line 85 correctly targets `COLUMN_TRANSACTION_INFO`. The bug is real and exactly as described.

Audit Report

## Title
Wrong Source Column in `migrate_transaction_info` Corrupts `COLUMN_TRANSACTION_INFO` During Migration — (File: `util/migrate/src/migrations/table_to_struct.rs`)

## Summary
The `migrate_transaction_info` function in `ChangeMoleculeTableToStruct` traverses `COLUMN_UNCLES` as its read source while writing into `COLUMN_TRANSACTION_INFO`. This means uncle-block data is stripped of 12 bytes and written into the transaction-info column under uncle keys, while actual `COLUMN_TRANSACTION_INFO` entries are never migrated out of the old Molecule table format. The corruption is silent, permanent, and stamped with the new DB version so it cannot be re-run.

## Finding Description
In `util/migrate/src/migrations/table_to_struct.rs` at line 93, the traversal call reads:

```rust
db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
```

The closure `transaction_info_migration` writes to `COLUMN_TRANSACTION_INFO` (line 85), but the source column is `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO`. Compare with the analogous `migrate_uncles` function (line 66), which correctly passes `COLUMN_UNCLES` as both source and write target. The `migrate_header` function (line 40) similarly uses `COLUMN_BLOCK_HEADER` for both. Only `migrate_transaction_info` has the mismatch.

Two simultaneous effects:
1. Uncle entries (keyed by block hash) are stripped of 12 bytes (not 16, since uncle size check uses `HEADER_SIZE = 240` — but here the check is `TRANSACTION_INFO_SIZE = 52`, so any uncle entry not exactly 52 bytes gets its first 16 bytes stripped and written into `COLUMN_TRANSACTION_INFO` under the uncle's block-hash key).
2. All real `COLUMN_TRANSACTION_INFO` entries remain in the old Molecule table format (with 16-byte size/offset header) that post-migration readers no longer account for.

The migration is called unconditionally in `migrate()` at line 159, and the DB version is stamped after completion, preventing re-execution.

## Impact Explanation
After migration, any read of `COLUMN_TRANSACTION_INFO` by transaction hash returns bytes still carrying the 16-byte Molecule table header. Code expecting the struct layout misparses `block_hash`, `block_number`, `block_epoch`, and `index`. Depending on error handling, this causes either silent wrong data returned via `get_transaction` / `get_transaction_info` RPC calls, or a deserialization panic that crashes the node. The minimum confirmed impact is **local RPC API crash** (Note: 0–500 points); if the malformed bytes cause a panic in the store read path, it rises to **node crash** (High: 10001–15000 points).

## Likelihood Explanation
The migration triggers automatically on node startup whenever the stored DB version predates `20200703124523`. Any operator upgrading from a pre-v0.35.0 database hits this path with no special configuration. No integrity check is performed before stamping the new version. The corruption is therefore guaranteed for any affected upgrade path and is not recoverable without manual DB repair.

## Recommendation
Change line 93 to traverse the correct source column:

```rust
// Before (wrong):
db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;

// After (correct):
db.traverse(COLUMN_TRANSACTION_INFO, &mut transaction_info_migration, mode, LIMIT)?;
```

Additionally, add a post-migration sanity check that reads back at least one `COLUMN_TRANSACTION_INFO` entry and verifies it decodes as a valid `TransactionInfo` struct before stamping the new DB version.

## Proof of Concept
1. Obtain a CKB database created before v0.35.0 containing at least one stored transaction and at least one uncle block.
2. Run `ckb migrate --force` (or start the node, which triggers background migration automatically).
3. After migration completes, call the `get_transaction` RPC for any transaction that existed before migration.
4. Observe that `block_hash` / `block_number` fields are wrong or deserialization fails, because the stored bytes still carry the 16-byte Molecule table header.
5. Directly iterate `COLUMN_TRANSACTION_INFO` and observe spurious entries keyed by block hashes (uncle keys) containing truncated uncle payloads — data that does not belong in this column.