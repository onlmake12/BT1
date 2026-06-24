The bug is confirmed in the actual code. Line 93 of `util/migrate/src/migrations/table_to_struct.rs` uses `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO` in the `db.traverse` call inside `migrate_transaction_info`. All other sub-migrations correctly traverse their own column. The downstream effects in `store/src/store.rs` (`get_transaction_info`, `get_transaction_with_info`) are also confirmed.

Audit Report

## Title
`migrate_transaction_info` Traverses `COLUMN_UNCLES` Instead of `COLUMN_TRANSACTION_INFO`, Leaving All Pre-Migration Transaction Info Records Uncoverted — (`File: util/migrate/src/migrations/table_to_struct.rs`)

## Summary
The `ChangeMoleculeTableToStruct` migration's `migrate_transaction_info` function calls `db.traverse(COLUMN_UNCLES, ...)` instead of `db.traverse(COLUMN_TRANSACTION_INFO, ...)`. As a result, every `TransactionInfo` record written in the old molecule Table encoding (68 bytes) is never converted to the new Struct encoding (52 bytes). Any node that ran this migration while holding pre-v0.35.0 data retains stale, mis-encoded transaction info for every historical transaction, causing corrupted or missing results for all downstream consumers of `get_transaction_info` and `get_transaction`.

## Finding Description
`ChangeMoleculeTableToStruct` (version `20200703124523`) rewrites four column families from molecule's variable-length Table layout to fixed-size Struct layout. Three sub-migrations correctly traverse their own column:

| Sub-migration | Column traversed | Column written |
|---|---|---|
| `migrate_header` | `COLUMN_BLOCK_HEADER` | `COLUMN_BLOCK_HEADER` ✓ |
| `migrate_uncles` | `COLUMN_UNCLES` | `COLUMN_UNCLES` ✓ |
| **`migrate_transaction_info`** | **`COLUMN_UNCLES`** ← **wrong** | `COLUMN_TRANSACTION_INFO` ✗ |
| `migrate_epoch_ext` | `COLUMN_EPOCH` | `COLUMN_EPOCH` ✓ |

The defective call is at line 93: [1](#0-0) 

The closure correctly targets `COLUMN_TRANSACTION_INFO` for writes: [2](#0-1) 

Because uncle header records are 240 bytes (not 52), the `value.len() != TRANSACTION_INFO_SIZE` guard is always true for uncle records, so the closure fires on every uncle entry — writing uncle-keyed data into `COLUMN_TRANSACTION_INFO` under uncle hashes. The actual `COLUMN_TRANSACTION_INFO` entries (keyed by tx hash) are never visited and remain in the old 68-byte Table format.

After migration, `get_transaction_info` deserializes these 68-byte records using fixed Struct offsets: [3](#0-2) 

`from_slice_should_be_ok` wraps the raw slice without length validation. Field accessors then read at wrong offsets: `block_number()` reads bytes `[0..8]` which contain the 4-byte `total_size` and first 4 bytes of the offset table (garbage); `block_epoch()` reads remaining offset bytes (garbage); `key()` reads shifted data. Every historical transaction's info is silently corrupted.

`get_transaction_with_info` then uses this corrupted `tx_info.key()` to look up `COLUMN_BLOCK_BODY`: [4](#0-3) 

The wrong key produces no match, so `get_transaction` returns `None` for any pre-migration confirmed transaction.

## Impact Explanation
This is a **Medium** impact: **Suboptimal/incorrect implementation of CKB state storage mechanism** (2001–10000 points). The `COLUMN_TRANSACTION_INFO` column is permanently left in the old Table encoding for all pre-migration transactions. All reads via `get_transaction_info` and `get_transaction` return corrupted or missing data. Additionally, uncle hashes are spuriously inserted into `COLUMN_TRANSACTION_INFO`, so `transaction_exists` returns `true` for uncle hashes (not transaction hashes), corrupting the existence index. The impact does not rise to Critical or High because it is local to nodes that ran the migration from pre-v0.35.0 data and does not directly cause network-wide crashes or consensus deviation.

## Likelihood Explanation
Any operator who upgraded a CKB node from a version prior to v0.35.0 (released July 2020) to v0.35.0 or later without re-syncing from genesis would have triggered this migration automatically on startup or via `ckb migrate`. The migration is deterministic and affects every `COLUMN_TRANSACTION_INFO` entry. No attacker action is required; the bug is triggered by the standard, documented upgrade procedure. The migration version `20200703124523` is stamped in the database after running, so affected nodes cannot re-run the migration without a new migration step. [5](#0-4) 

## Recommendation
Change line 93 of `util/migrate/src/migrations/table_to_struct.rs` from:
```rust
db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
```
to:
```rust
db.traverse(COLUMN_TRANSACTION_INFO, &mut transaction_info_migration, mode, LIMIT)?;
```

This mirrors the correct pattern used by `migrate_header` (traverses `COLUMN_BLOCK_HEADER`) and `migrate_uncles` (traverses `COLUMN_UNCLES`). [6](#0-5) 

Because the migration version is already stamped in affected databases, a new migration (with a new version string) must be added to re-scan `COLUMN_TRANSACTION_INFO` and rewrite any remaining 68-byte Table-format entries to the 52-byte Struct format, and to remove the spuriously inserted uncle-hash entries from `COLUMN_TRANSACTION_INFO`.

## Proof of Concept
1. Start a CKB node on any version prior to v0.35.0 and sync at least one block (so `COLUMN_TRANSACTION_INFO` has entries in 68-byte Table format).
2. Upgrade the binary to v0.35.0+ and run `ckb migrate` (or start the node, which auto-migrates). The migration completes and stamps version `20200703124523`.
3. Query any pre-migration transaction via RPC: `get_transaction(<tx_hash>)`.
4. Observe `null` returned despite the transaction being confirmed on-chain, because `get_transaction_with_info` resolves a corrupted `key` from the mangled `TransactionInfo` and fails to find the block body entry.
5. Conversely, query `transaction_exists(<uncle_hash>)` — observe `true` returned for an uncle hash, because the migration wrote uncle-keyed data into `COLUMN_TRANSACTION_INFO`.

### Citations

**File:** util/migrate/src/migrations/table_to_struct.rs (L12-12)
```rust
const VERSION: &str = "20200703124523";
```

**File:** util/migrate/src/migrations/table_to_struct.rs (L39-41)
```rust
            let (_count, nk) =
                db.traverse(COLUMN_BLOCK_HEADER, &mut header_view_migration, mode, LIMIT)?;
            next_key = nk;
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

**File:** store/src/store.rs (L337-342)
```rust
        self.get(COLUMN_BLOCK_BODY, tx_info.key().as_slice())
            .map(|slice| {
                let reader = packed::TransactionViewReader::from_slice_should_be_ok(slice.as_ref());
                (reader.into(), tx_info)
            })
    }
```
