The bug is confirmed by the actual code. Line 93 of `util/migrate/src/migrations/table_to_struct.rs` unambiguously traverses `COLUMN_UNCLES` inside `migrate_transaction_info` instead of `COLUMN_TRANSACTION_INFO`. All other sibling functions correctly traverse their own column.

Audit Report

## Title
Wrong Column Traversed in `migrate_transaction_info` Corrupts `COLUMN_TRANSACTION_INFO` After Migration - (File: `util/migrate/src/migrations/table_to_struct.rs`)

## Summary
The `ChangeMoleculeTableToStruct` migration (`version 20200703124523`) contains a copy-paste error in `migrate_transaction_info`: it iterates over `COLUMN_UNCLES` (column `"11"`) instead of `COLUMN_TRANSACTION_INFO` (column `"5"`). As a result, `COLUMN_TRANSACTION_INFO` is never migrated from Table to Struct format, and uncle-header bytes are injected into it keyed by uncle hashes. Any node that ran this migration while upgrading from a pre-v0.35.0 database has a permanently corrupted transaction-info column with no error surfaced.

## Finding Description
In `migrate_transaction_info` at line 93:

```rust
let (_count, nk) =
    db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
```

The closure `transaction_info_migration` correctly writes to `COLUMN_TRANSACTION_INFO` and strips 16 bytes, but it is fed records from `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO`. Compare with the three sibling functions:

- `migrate_header` (line 40): traverses `COLUMN_BLOCK_HEADER`, writes to `COLUMN_BLOCK_HEADER`
- `migrate_uncles` (line 66): traverses `COLUMN_UNCLES`, writes to `COLUMN_UNCLES`
- `migrate_epoch_ext` (line 120): traverses `COLUMN_EPOCH`, writes to `COLUMN_EPOCH`

The size guard `value.len() != TRANSACTION_INFO_SIZE` (52 bytes) is always true for uncle header records (~240 bytes old format, ~228 bytes new format), so every uncle record passes the guard. The migration writes `uncle_header_data[16..]` into `COLUMN_TRANSACTION_INFO` keyed by uncle block hashes. Meanwhile, the actual `COLUMN_TRANSACTION_INFO` records are never touched and remain in old 68-byte Table format (16-byte header + 52-byte payload). Post-migration code in `get_transaction_info` calls `packed::TransactionInfoReader::from_slice_should_be_ok` on these 68-byte values, interpreting the 16-byte table header as the start of the Struct, producing a completely wrong `block_hash`, `block_number`, and index. Existing guards are insufficient: the migration returns `Ok(())` with no error, the version key is stamped, and the migration never re-runs.

## Impact Explanation
After migration:
1. `get_transaction` / `get_transaction_with_info` returns `None` or wrong data for all pre-migration transactions because `get_transaction_info` extracts a garbage `block_hash` from the un-migrated Table-format bytes, and the subsequent block-body lookup fails.
2. `transaction_exists` returns `true` for uncle hashes (false positive) and `false` for real pre-migration transaction hashes (false negative), breaking any logic depending on this check.
3. RPC `get_transaction` returns null for any historical transaction, breaking block explorers, wallets, and RPC callers.

This constitutes a **suboptimal (corrupted) implementation of CKB state storage mechanism** — fitting the **Medium (2001–10000 points)** bounty impact. The corruption is silent, permanent (version key is stamped), and affects the full historical transaction index of any node that ran this migration.

## Likelihood Explanation
The migration runs automatically on any node upgrading from a database created before v0.35.0 (July 2020). It completes without error or panic, giving the operator no indication of corruption. The version key is stamped immediately, so the migration never re-runs. Any operator restoring from an old pre-v0.35.0 backup and upgrading to a modern binary would be silently affected. No attacker capability is required — the bug is triggered by the normal upgrade path.

## Recommendation
Fix line 93 of `util/migrate/src/migrations/table_to_struct.rs` to traverse `COLUMN_TRANSACTION_INFO` instead of `COLUMN_UNCLES`:

```rust
// Before (wrong):
let (_count, nk) =
    db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;

// After (correct):
let (_count, nk) =
    db.traverse(COLUMN_TRANSACTION_INFO, &mut transaction_info_migration, mode, LIMIT)?;
```

Add a regression test that seeds both `COLUMN_TRANSACTION_INFO` and `COLUMN_UNCLES` with pre-migration Table-format records, runs the migration, and asserts: (a) all `COLUMN_TRANSACTION_INFO` records are in 52-byte Struct format, and (b) no uncle-hash keys appear in `COLUMN_TRANSACTION_INFO`.

## Proof of Concept
1. Create a RocksDB database with `COLUMN_TRANSACTION_INFO` populated with 68-byte Table-format records (16-byte header + 52-byte payload) and `COLUMN_UNCLES` populated with 240-byte uncle-header records, with no migration version key set.
2. Run `ChangeMoleculeTableToStruct::migrate`.
3. Observe: migration returns `Ok(db)` with no error.
4. Inspect `COLUMN_TRANSACTION_INFO`: original 68-byte records are unchanged (not migrated); uncle-hash keys now exist with 224-byte values (`uncle_header[16..]`).
5. Call `get_transaction_info(tx_hash)` for any pre-migration transaction hash: the returned `TransactionInfo` has a garbage `block_hash` (parsed from the 16-byte table header).
6. Call `transaction_exists(uncle_hash)` for any uncle hash: returns `true` (false positive).
7. Call RPC `get_transaction` with a pre-migration transaction hash: returns `null`. [1](#0-0) [2](#0-1) [3](#0-2)

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
