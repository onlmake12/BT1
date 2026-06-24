Audit Report

## Title
Wrong Column Traversal in `migrate_transaction_info` Leaves `COLUMN_TRANSACTION_INFO` Unmigrated, Causing Node Panic on Any Transaction Lookup — (`util/migrate/src/migrations/table_to_struct.rs`)

## Summary

`ChangeMoleculeTableToStruct::migrate_transaction_info()` traverses `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO` at line 93. This leaves all pre-migration 68-byte table-format transaction info entries untouched in `COLUMN_TRANSACTION_INFO`. After the migration bumps the DB version, any call to `get_transaction_info()` reads a 68-byte entry and passes it to `from_slice_should_be_ok`, which unconditionally panics because `TransactionInfoReader::verify` requires exactly 52 bytes. The node process crashes and cannot recover without manual DB repair or re-sync.

## Finding Description

**Root cause — line 93:**

```rust
let (_count, nk) =
    db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
//              ^^^^^^^^^^^^^ BUG: should be COLUMN_TRANSACTION_INFO
``` [1](#0-0) 

Every other sub-migration correctly names its own column — `migrate_header` traverses `COLUMN_BLOCK_HEADER`, `migrate_uncles` traverses `COLUMN_UNCLES`, `migrate_epoch_ext` traverses `COLUMN_EPOCH`. [2](#0-1) [3](#0-2) [4](#0-3) 

**Execution sequence:**

1. `migrate_uncles()` runs first (line 153) and rewrites uncle `HeaderView` entries from 240 bytes → 228 bytes in `COLUMN_UNCLES`. [5](#0-4) 

2. `migrate_transaction_info()` then traverses `COLUMN_UNCLES` (now containing 228-byte entries). The guard `if value.len() != TRANSACTION_INFO_SIZE (52)` is true for every uncle entry (228 ≠ 52), so the closure writes `uncle_value[16..]` (212 bytes) into `COLUMN_TRANSACTION_INFO` under each uncle's hash key. [6](#0-5) 

3. The actual `COLUMN_TRANSACTION_INFO` entries (68-byte old table format) are **never read or rewritten**.

4. Migration returns `Ok(db)` and the DB version is bumped to `"20200703124523"`. [7](#0-6) 

**Panic path after restart:**

`get_transaction_info` reads from `COLUMN_TRANSACTION_INFO` and calls `from_slice_should_be_ok` on the raw bytes: [8](#0-7) 

`from_slice_should_be_ok` unconditionally panics on any verification error: [9](#0-8) 

`TransactionInfoReader::verify` (a fixed-size struct) rejects any slice that is not exactly 52 bytes. The unmigrated entries are 68 bytes (52 + 16 bytes of molecule table overhead), so `verify` returns `TotalSizeNotMatch(52, 68)` and the panic fires:

```
failed to convert from slice: reason: TotalSizeNotMatch(52, 68); data: 0x<68 bytes>
```

## Impact Explanation

Any node that ran `ckb migrate` while upgrading from a pre-v0.35.0 database will have an unmigrated `COLUMN_TRANSACTION_INFO`. After the upgrade, the node process panics (crashes) on the first call to `get_transaction_info`, which is triggered by:

- RPC calls: `get_transaction`, any verbosity-level transaction query
- Chain sync and block attachment paths that look up committed transactions
- Any tx-pool or script verification path that checks whether a transaction is already committed

The node is rendered permanently inoperable until the DB is manually repaired or re-synced from scratch. This matches **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

The precondition is trivially met on any real mainnet or testnet node: uncle blocks exist in every epoch, and their serialized `HeaderView` size (228 bytes post-migration) is never 52 bytes. Any node operator who ran `ckb migrate` for the v0.35.0 upgrade would have hit this. After migration, any external user can trigger the crash by issuing a single `get_transaction` RPC call with any valid committed transaction hash — no special privileges required.

## Recommendation

Fix line 93 to traverse `COLUMN_TRANSACTION_INFO` instead of `COLUMN_UNCLES`:

```rust
let (_count, nk) =
    db.traverse(COLUMN_TRANSACTION_INFO, &mut transaction_info_migration, mode, LIMIT)?;
``` [1](#0-0) 

Additionally, add a follow-up repair migration for nodes that already ran the broken migration: detect 68-byte entries in `COLUMN_TRANSACTION_INFO` and rewrite them, and remove the spurious 212-byte uncle-keyed entries that were incorrectly written.

## Proof of Concept

1. Start a CKB node on a pre-v0.35.0 DB that has at least one uncle block (true of any mainnet/testnet node).
2. Run `ckb migrate`.
3. Observe migration completes with no error and DB version is bumped to `"20200703124523"`.
4. Restart the node and call `ckb rpc get_transaction --hash <any_committed_tx_hash>`.
5. Node panics: `failed to convert from slice: reason: TotalSizeNotMatch(52, 68); data: 0x...`
6. Confirm by dumping `COLUMN_TRANSACTION_INFO` directly: entries still have 68-byte values (old table format), plus spurious entries with uncle hashes as keys containing 212-byte slices.

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

**File:** util/migrate/src/migrations/table_to_struct.rs (L119-121)
```rust
            let mode = self.mode(&next_key);
            let (_count, nk) = db.traverse(COLUMN_EPOCH, &mut epoch_ext_migration, mode, LIMIT)?;
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
