The code is confirmed. Let me verify the exact lines and behavior.

The bug is confirmed in the actual code. The `migrate_transaction_info` function at line 93 traverses `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO`. The `TransactionInfoReader` field accessors are confirmed at fixed offsets (`TOTAL_SIZE = 52`), and `from_slice_should_be_ok` wraps the raw slice without length validation, meaning old 68-byte Table-format entries are silently misread.

Audit Report

## Title
`migrate_transaction_info` Traverses `COLUMN_UNCLES` Instead of `COLUMN_TRANSACTION_INFO`, Leaving All Pre-Migration Transaction Info Corrupted — (`File: util/migrate/src/migrations/table_to_struct.rs`)

## Summary
The `ChangeMoleculeTableToStruct` migration's `migrate_transaction_info` function calls `db.traverse(COLUMN_UNCLES, ...)` at line 93 instead of `db.traverse(COLUMN_TRANSACTION_INFO, ...)`. As a result, every `TransactionInfo` record written in the old molecule Table encoding (68 bytes) is never converted to the new Struct encoding (52 bytes). Any node that ran this migration while holding pre-v0.35.0 data retains stale, mis-encoded transaction info for every historical transaction, causing `get_transaction` to silently return `None` for all pre-migration confirmed transactions.

## Finding Description

`ChangeMoleculeTableToStruct` (version `20200703124523`) rewrites four column families from molecule's variable-length Table layout to fixed-size Struct layout. Three sub-migrations correctly traverse their own column. The fourth does not: [1](#0-0) 

Line 93 passes `COLUMN_UNCLES` to `db.traverse`, but the closure writes to `COLUMN_TRANSACTION_INFO`. Uncle header records are 240 bytes; `TRANSACTION_INFO_SIZE` is 52. Since `240 != 52`, the size guard is always true for uncle records, so the closure fires for every uncle — writing 224 bytes of uncle header data into `COLUMN_TRANSACTION_INFO` under uncle block hashes. The actual `COLUMN_TRANSACTION_INFO` entries (keyed by tx hash, 68 bytes in old Table format) are never visited and remain unconverted.

Compare with the correct pattern used by `migrate_header` and `migrate_uncles`: [2](#0-1) [3](#0-2) 

After migration, `get_transaction_info` reads the unconverted 68-byte Table-format entry using `from_slice_should_be_ok`, which wraps the raw slice without length validation: [4](#0-3) 

`TransactionInfoReader` field accessors read at fixed Struct offsets: [5](#0-4) 

Applied to the old 68-byte Table format:
- `block_number()` → `[0..8]` → reads `total_size` (4 bytes) + first offset table entry (4 bytes) = garbage
- `block_epoch()` → `[8..16]` → reads remaining offset table bytes = garbage
- `key()` → `[16..52]` → reads actual `block_number || block_epoch || key[0..20]` = completely wrong `block_hash`

`get_transaction_with_info` then uses this corrupted `block_hash` to look up `COLUMN_BLOCK_BODY`: [6](#0-5) 

The lookup finds no match and returns `None`, so `get_transaction` returns `None` for every pre-migration confirmed transaction.

The conversion path that extracts `block_hash` from `key()` is confirmed: [7](#0-6) 

## Impact Explanation

This is a **Medium** impact: **Suboptimal/incorrect implementation of CKB state storage mechanism** (2001–10000 points).

The migration permanently corrupts `COLUMN_TRANSACTION_INFO` for all nodes that upgraded from pre-v0.35.0:
1. All pre-migration `TransactionInfo` entries remain in the old 68-byte Table format, silently misread as 52-byte Struct, producing garbage `block_hash` and `key` values.
2. `get_transaction` / `get_transaction_with_info` return `None` for every pre-migration confirmed transaction — wallets, explorers, and dApps receive incorrect "transaction not found" for confirmed on-chain transactions.
3. `COLUMN_TRANSACTION_INFO` is additionally polluted with uncle-hash-keyed garbage entries (224 bytes each), causing `transaction_exists` to return `true` for uncle block hashes.

This does not directly crash the network or cause consensus deviation (the corrupted data is in a read-only lookup index, not in block validation state), placing it firmly in the Medium storage-mechanism category rather than Critical/High.

## Likelihood Explanation

The migration is triggered automatically on node startup when the database version is behind, or explicitly via `ckb migrate`. Any operator who upgraded a node from a version prior to v0.35.0 (released July 2020) to v0.35.0 or later would have run this migration. No attacker action or special configuration is required — the bug is deterministic and affects every pre-migration `TransactionInfo` record on every affected node. Nodes that synced from scratch on v0.35.0+ are unaffected.

## Recommendation

Change line 93 of `util/migrate/src/migrations/table_to_struct.rs` from:

```rust
db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
```

to:

```rust
db.traverse(COLUMN_TRANSACTION_INFO, &mut transaction_info_migration, mode, LIMIT)?;
``` [8](#0-7) 

Because the migration version `20200703124523` is already stamped in affected databases, a new migration (with a new version string) must also be added to re-scan `COLUMN_TRANSACTION_INFO` and rewrite any remaining 68-byte Table-format entries to the 52-byte Struct format, and remove the spurious uncle-hash-keyed entries written by the buggy migration.

## Proof of Concept

1. Start a CKB node on any version prior to v0.35.0 and sync at least one block (so `COLUMN_TRANSACTION_INFO` has entries in 68-byte Table format).
2. Upgrade the binary to v0.35.0+ and run `ckb migrate` (or start the node, which auto-migrates). Migration completes and stamps version `20200703124523`.
3. Inspect `COLUMN_TRANSACTION_INFO` directly via RocksDB: all original tx-hash-keyed entries remain at 68 bytes (unconverted); new uncle-hash-keyed entries of 224 bytes are present.
4. Call `get_transaction(<any_pre_migration_tx_hash>)` via RPC.
5. Observe `null` returned despite the transaction being confirmed on-chain, because `get_transaction_with_info` reconstructs a garbage `block_hash` from the misread 68-byte entry and fails to find the block body in `COLUMN_BLOCK_BODY`.

### Citations

**File:** util/migrate/src/migrations/table_to_struct.rs (L39-41)
```rust
            let (_count, nk) =
                db.traverse(COLUMN_BLOCK_HEADER, &mut header_view_migration, mode, LIMIT)?;
            next_key = nk;
```

**File:** util/migrate/src/migrations/table_to_struct.rs (L65-67)
```rust
            let mode = self.mode(&next_key);
            let (_count, nk) = db.traverse(COLUMN_UNCLES, &mut uncles_migration, mode, LIMIT)?;
            next_key = nk;
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

**File:** store/src/store.rs (L337-341)
```rust
        self.get(COLUMN_BLOCK_BODY, tx_info.key().as_slice())
            .map(|slice| {
                let reader = packed::TransactionViewReader::from_slice_should_be_ok(slice.as_ref());
                (reader.into(), tx_info)
            })
```

**File:** util/gen-types/src/generated/extensions.rs (L5620-5628)
```rust
    pub fn block_number(&self) -> Uint64Reader<'r> {
        Uint64Reader::new_unchecked(&self.as_slice()[0..8])
    }
    pub fn block_epoch(&self) -> Uint64Reader<'r> {
        Uint64Reader::new_unchecked(&self.as_slice()[8..16])
    }
    pub fn key(&self) -> TransactionKeyReader<'r> {
        TransactionKeyReader::new_unchecked(&self.as_slice()[16..52])
    }
```

**File:** util/types/src/conversion/storage.rs (L349-357)
```rust
impl<'r> From<packed::TransactionInfoReader<'r>> for core::TransactionInfo {
    fn from(value: packed::TransactionInfoReader<'r>) -> core::TransactionInfo {
        core::TransactionInfo {
            block_hash: value.key().block_hash().to_entity(),
            index: value.key().index().into(),
            block_number: value.block_number().into(),
            block_epoch: value.block_epoch().into(),
        }
    }
```
