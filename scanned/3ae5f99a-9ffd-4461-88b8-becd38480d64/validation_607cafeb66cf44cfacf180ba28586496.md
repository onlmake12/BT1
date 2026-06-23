I spotted something in `table_to_struct.rs`. Let me verify it carefully.

### Title
Wrong Column Traversed in `migrate_transaction_info` Corrupts `COLUMN_TRANSACTION_INFO` After Migration - (File: `util/migrate/src/migrations/table_to_struct.rs`)

---

### Summary

The `ChangeMoleculeTableToStruct` database migration (`v0.35.0`, version key `20200703124523`) contains a copy-paste error in `migrate_transaction_info`: it iterates over `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO`. This is a direct structural analog to the ReseedField.sol bug — the wrong storage slot is read/written during a critical migration, leaving the intended column in its old (unparseable) format and injecting garbage entries from a different column into it.

---

### Finding Description

In `util/migrate/src/migrations/table_to_struct.rs`, the `migrate_transaction_info` function is responsible for converting all `COLUMN_TRANSACTION_INFO` records from the old Molecule **Table** encoding (which has a 16-byte header: 4-byte total-size + 3 × 4-byte field offsets) to the new Molecule **Struct** encoding (fixed-size, no header).

The function correctly writes to `COLUMN_TRANSACTION_INFO` and correctly strips 16 bytes from the front of each value, but it **reads from `COLUMN_UNCLES` instead of `COLUMN_TRANSACTION_INFO`**:

```rust
// util/migrate/src/migrations/table_to_struct.rs  lines 77-101
fn migrate_transaction_info(&self, db: &RocksDB) -> Result<()> {
    const TRANSACTION_INFO_SIZE: usize = 52;
    ...
    let mut transaction_info_migration = |key: &[u8], value: &[u8]| -> Result<()> {
        if value.len() != TRANSACTION_INFO_SIZE {
            wb.put(COLUMN_TRANSACTION_INFO, key, &value[16..])?;  // writes to correct column
        }
        Ok(())
    };
    ...
    let (_count, nk) =
        db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;  // ← BUG: reads UNCLES, not TRANSACTION_INFO
    ...
}
``` [1](#0-0) 

Compare with the three sibling functions that all correctly read from their own column:

- `migrate_header` traverses `COLUMN_BLOCK_HEADER` and writes to `COLUMN_BLOCK_HEADER` [2](#0-1) 
- `migrate_uncles` traverses `COLUMN_UNCLES` and writes to `COLUMN_UNCLES` [3](#0-2) 
- `migrate_epoch_ext` traverses `COLUMN_EPOCH` and writes to `COLUMN_EPOCH` [4](#0-3) 

`COLUMN_UNCLES` is column `"11"` and `COLUMN_TRANSACTION_INFO` is column `"5"` — entirely different key spaces. [5](#0-4) 

The double effect of the bug:

1. **`COLUMN_TRANSACTION_INFO` is never migrated.** All pre-migration transaction info records remain in old Table format (68 bytes: 16-byte header + 52-byte payload). Post-migration code reads them with `packed::TransactionInfoReader::from_slice_should_be_ok`, which interprets the first 52 bytes as a Struct — meaning it reads the 16-byte table header as the start of `block_hash`, producing a completely wrong `TransactionInfo` (wrong block hash, wrong block number, wrong index). [6](#0-5) 

2. **`COLUMN_TRANSACTION_INFO` is polluted with uncle-header data.** Uncle header values are never 52 bytes (they are 240 bytes in old format, 228 in new), so the `value.len() != TRANSACTION_INFO_SIZE` guard is always true. The migration writes `uncle_header_data[16..]` into `COLUMN_TRANSACTION_INFO` keyed by uncle block hashes. This causes `transaction_exists(uncle_hash)` to return `true` for uncle hashes, and `get_transaction_info(uncle_hash)` to return garbage parsed as `TransactionInfo`. [7](#0-6) 

---

### Impact Explanation

After a node runs this migration (upgrading from a pre-v0.35.0 database):

- **`get_transaction` / `get_transaction_with_info`** — returns `None` for all pre-migration transactions because the wrong `block_hash` is used to look up the block body. [8](#0-7) 
- **`transaction_exists`** — returns `false` for real pre-migration transactions and `true` for uncle hashes, breaking any logic that depends on this check. [7](#0-6) 
- **RPC `get_transaction`** — returns null/error for any historical transaction, breaking block explorers, wallets, and any RPC caller querying pre-migration transactions.
- **Sync / relay** — a syncing peer requesting transaction proofs or historical data from this node receives wrong or missing data, causing silent chain-data divergence.
- **`attach_block` / `detach_block`** — new blocks write correct Struct-format entries, but old entries remain in Table format, creating a mixed-format column that is permanently inconsistent. [9](#0-8) 

---

### Likelihood Explanation

The migration runs automatically on any node that upgrades from a database created before v0.35.0 (July 2020). The migration completes without error (no panic, no `Err` return), so the operator has no indication that data is corrupted. The version key is stamped as migrated, so the migration never re-runs. Any node operator who kept an old database and upgraded to a modern binary is silently affected. The bug is present in the current codebase and would affect any future operator in the same situation (e.g., restoring from an old backup).

---

### Recommendation

Fix line 93 of `util/migrate/src/migrations/table_to_struct.rs` to traverse `COLUMN_TRANSACTION_INFO` instead of `COLUMN_UNCLES`:

```rust
// Before (wrong):
let (_count, nk) =
    db.traverse(COLUMN_UNCLES, &mut transaction_info_migration, mode, LIMIT)?;

// After (correct):
let (_count, nk) =
    db.traverse(COLUMN_TRANSACTION_INFO, &mut transaction_info_migration, mode, LIMIT)?;
``` [10](#0-9) 

Additionally, add a regression test that verifies `COLUMN_TRANSACTION_INFO` records are correctly converted and that `COLUMN_UNCLES` records are not written into `COLUMN_TRANSACTION_INFO`.

---

### Proof of Concept

1. Start a CKB node with a database created before v0.35.0 (version key absent or `< 20200703124523`).
2. Upgrade the binary and run `ckb migrate` (or start the node, which auto-migrates).
3. Migration completes successfully with no errors.
4. Call RPC `get_transaction` with any transaction hash that existed before the migration.
5. Observe: the RPC returns `null` because `get_transaction_info` parses the un-migrated Table-format bytes as a Struct, extracts a garbage `block_hash`, and the subsequent block-body lookup fails.
6. Call `transaction_exists(uncle_hash)` for any uncle block hash from the pre-migration chain.
7. Observe: returns `true` (false positive) because the migration wrote uncle header data into `COLUMN_TRANSACTION_INFO` keyed by uncle hashes.

The root cause is confirmed at: [10](#0-9)

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

**File:** util/migrate/src/migrations/table_to_struct.rs (L119-121)
```rust
            let mode = self.mode(&next_key);
            let (_count, nk) = db.traverse(COLUMN_EPOCH, &mut epoch_ext_migration, mode, LIMIT)?;
            next_key = nk;
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

**File:** store/src/store.rs (L292-298)
```rust
    /// Returns true if the transaction confirmed in main chain.
    ///
    /// This function is base on transaction index `COLUMN_TRANSACTION_INFO`.
    /// Current release maintains a full index of historical transaction by default, this may be changed in future
    fn transaction_exists(&self, hash: &packed::Byte32) -> bool {
        self.get(COLUMN_TRANSACTION_INFO, hash.as_slice()).is_some()
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

**File:** store/src/store.rs (L315-342)
```rust
    /// Gets transaction and associated info with correspond hash
    fn get_transaction_with_info(
        &self,
        hash: &packed::Byte32,
    ) -> Option<(TransactionView, TransactionInfo)> {
        let tx_info = self.get_transaction_info(hash)?;
        if let Some(freezer) = self.freezer()
            && tx_info.block_number > 0
            && tx_info.block_number < freezer.number()
        {
            let raw_block = freezer
                .retrieve(tx_info.block_number)
                .expect("block frozen")?;
            let raw_block_reader =
                packed::BlockReader::from_compatible_slice(&raw_block).expect("checked data");
            if raw_block_reader.calc_header_hash().as_slice() == tx_info.block_hash.as_slice()
                && let Some(tx_reader) = raw_block_reader.transactions().get(tx_info.index)
                && tx_reader.calc_tx_hash().as_slice() == hash.as_slice()
            {
                return Some((tx_reader.to_entity().into_view(), tx_info));
            }
        }
        self.get(COLUMN_BLOCK_BODY, tx_info.key().as_slice())
            .map(|slice| {
                let reader = packed::TransactionViewReader::from_slice_should_be_ok(slice.as_ref());
                (reader.into(), tx_info)
            })
    }
```

**File:** store/src/transaction.rs (L255-293)
```rust
    pub fn attach_block(&self, block: &BlockView) -> Result<(), Error> {
        let header = block.data().header();
        let block_hash = block.hash();
        for (index, tx_hash) in block.tx_hashes().iter().enumerate() {
            let key = packed::TransactionKey::new_builder()
                .block_hash(block_hash.clone())
                .index(index)
                .build();
            let info = packed::TransactionInfo::new_builder()
                .key(key)
                .block_number(header.raw().number())
                .block_epoch(header.raw().epoch())
                .build();
            self.insert_raw(COLUMN_TRANSACTION_INFO, tx_hash.as_slice(), info.as_slice())?;
        }
        let block_number: packed::Uint64 = block.number().into();
        self.insert_raw(COLUMN_INDEX, block_number.as_slice(), block_hash.as_slice())?;
        for uncle in block.uncles().into_iter() {
            self.insert_raw(
                COLUMN_UNCLES,
                uncle.hash().as_slice(),
                Into::<packed::HeaderView>::into(uncle.header()).as_slice(),
            )?;
        }
        self.insert_raw(COLUMN_INDEX, block_hash.as_slice(), block_number.as_slice())
    }

    /// Detaches a block from the main chain, removing its transaction and uncle indices.
    pub fn detach_block(&self, block: &BlockView) -> Result<(), Error> {
        for tx_hash in block.tx_hashes().iter() {
            self.delete(COLUMN_TRANSACTION_INFO, tx_hash.as_slice())?;
        }
        for uncle in block.uncles().into_iter() {
            self.delete(COLUMN_UNCLES, uncle.hash().as_slice())?;
        }
        let block_number = block.data().header().raw().number();
        self.delete(COLUMN_INDEX, block_number.as_slice())?;
        self.delete(COLUMN_INDEX, block.hash().as_slice())
    }
```
