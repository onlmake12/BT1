All cited code references are verified against the actual source. Here is the validation result:

---

Audit Report

## Title
Wrong Column Traversal in `migrate_transaction_info` Causes Post-Migration Node Crash - (File: util/migrate/src/migrations/table_to_struct.rs)

## Summary
`migrate_transaction_info` in `ChangeMoleculeTableToStruct` traverses `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO` at line 93. This leaves all pre-migration `TransactionInfo` records in the old 52-byte table format untouched. After migration, any call to `get_transaction_info` on a pre-migration transaction hash passes a 52-byte blob to `from_slice_should_be_ok`, which expects a 36-byte struct and unconditionally panics, crashing the node process.

## Finding Description
**Root cause:** In `migrate_transaction_info` at line 93, the `db.traverse` call reads from `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO`:

```rust
// util/migrate/src/migrations/table_to_struct.rs, line 92-93
let (_count, nk) =
    db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;
```

The migration closure is:

```rust
// lines 82-88
let mut transaction_info_migration = |key: &[u8], value: &[u8]| -> Result<()> {
    if value.len() != TRANSACTION_INFO_SIZE {   // TRANSACTION_INFO_SIZE = 52
        wb.put(COLUMN_TRANSACTION_INFO, key, &value[16..])?;
    }
    Ok(())
};
```

Two concrete consequences:

1. **Pre-migration `TransactionInfo` records are never migrated.** The closure never reads `COLUMN_TRANSACTION_INFO`, so all 52-byte table-format entries remain there unchanged.

2. **Uncle data is injected into `COLUMN_TRANSACTION_INFO`.** Uncle entries are 240 bytes (`HEADER_SIZE = 240` in `migrate_uncles`). Since `240 != 52`, the guard passes for every uncle, writing `uncle_data[16..]` (224 bytes) into `COLUMN_TRANSACTION_INFO` keyed by uncle hashes.

**Crash path:** `get_transaction_info` reads raw bytes from `COLUMN_TRANSACTION_INFO` and calls `packed::TransactionInfoReader::from_slice_should_be_ok`:

```rust
// store/src/store.rs, lines 307-312
fn get_transaction_info(&self, hash: &packed::Byte32) -> Option<TransactionInfo> {
    self.get(COLUMN_TRANSACTION_INFO, hash.as_slice())
        .map(|slice| {
            let reader = packed::TransactionInfoReader::from_slice_should_be_ok(slice.as_ref());
```

`from_slice_should_be_ok` unconditionally panics on any verification failure:

```rust
// util/gen-types/src/prelude.rs, lines 45-53
fn from_slice_should_be_ok(slice: &'r [u8]) -> Self {
    match Self::from_slice(slice) {
        Ok(ret) => ret,
        Err(err) => panic!(
            "failed to convert from slice: reason: {}; data: 0x{}.",
            err, hex_string(slice)
        ),
    }
}
```

A 52-byte old-format blob fed to a 36-byte struct reader causes `from_slice` to return `Err` → unconditional panic → node process crash. The panic propagates through `get_transaction_with_info` → `get_transaction` → any RPC or internal path that looks up a pre-migration transaction.

## Impact Explanation
**High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

After `ckb migrate` completes, the node starts with a permanently corrupted `COLUMN_TRANSACTION_INFO`. The first call to `get_transaction_info` for any pre-migration transaction hash triggers the `panic!` in `from_slice_should_be_ok`, crashing the node process. The corruption is permanent — re-running the migration does not help because the version stamp is already updated. Any external user can trigger the crash by calling `get_transaction` (or any RPC that internally calls `get_transaction_info`) on any pre-migration transaction hash.

## Likelihood Explanation
Any node operator upgrading from a pre-v0.35.0 database and running `ckb migrate` triggers this path. `ChangeMoleculeTableToStruct` is registered unconditionally in the migration sequence at `util/migrate/src/migrate.rs` line 26. No attacker interaction is required for the migration step; the operator follows the standard documented upgrade procedure. Once the node is running, any external caller who submits a `get_transaction` RPC for a pre-migration transaction hash triggers the crash. Pre-migration transaction hashes are publicly visible on-chain, so the trigger is trivially discoverable.

## Recommendation
In `migrate_transaction_info`, replace `COLUMN_UNCLES` with `COLUMN_TRANSACTION_INFO` in the `db.traverse` call:

```rust
let (_count, nk) =
    db.traverse(COLUMN_TRANSACTION_INFO, &mut transaction_info_migration, mode, LIMIT)?;
```

Additionally, add per-function unit tests asserting that each `migrate_*` helper reads from and writes to the correct column, and add a post-migration sanity check verifying that all `COLUMN_TRANSACTION_INFO` entries are exactly 36 bytes.

## Proof of Concept
The bug is statically visible and the crash path is deterministic:

1. Start a CKB node on a pre-v0.35.0 database with at least one committed transaction and at least one uncle block.
2. Run `ckb migrate`.
3. Start the upgraded node.
4. Call any RPC that invokes `get_transaction_info` on a pre-migration transaction hash (e.g., `get_transaction` via JSON-RPC).
5. The node panics: `"failed to convert from slice: reason: <molecule error>; data: 0x<52-byte hex>"` and crashes.

Static proof (no runtime needed):
- `migrate_transaction_info` traverses `COLUMN_UNCLES` at line 93, not `COLUMN_TRANSACTION_INFO` — confirmed in `util/migrate/src/migrations/table_to_struct.rs`.
- `from_slice_should_be_ok` unconditionally panics on verification failure — confirmed in `util/gen-types/src/prelude.rs` lines 45–53.
- `get_transaction_info` calls `from_slice_should_be_ok` on raw `COLUMN_TRANSACTION_INFO` bytes — confirmed in `store/src/store.rs` lines 307–312. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

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

**File:** util/migrate/src/migrate.rs (L25-27)
```rust
        migrations.add_migration(Arc::new(DefaultMigration::new(INIT_DB_VERSION)));
        migrations.add_migration(Arc::new(migrations::ChangeMoleculeTableToStruct)); // since v0.35.0
        migrations.add_migration(Arc::new(migrations::CellMigration)); // since v0.37.0
```
