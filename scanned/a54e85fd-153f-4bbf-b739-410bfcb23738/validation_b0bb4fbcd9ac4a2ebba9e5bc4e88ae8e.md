### Title
Unconditional `.expect()` on `txs_sizes` in `FeeRateCollector::statistics` Panics on Nodes with Pre-v0.106 `BlockExt` Records — (`rpc/src/util/fee_rate.rs`)

### Summary

`FeeRateCollector::statistics` calls `.expect()` unconditionally on `block_ext.txs_sizes` for every block in the fee-rate window. Nodes that were upgraded from a version prior to v0.106 retain historical blocks stored in the old 5-field `BlockExt` schema, which deserialises with `txs_sizes: None`. Any unprivileged caller invoking `get_fee_rate_statistics` with a window that overlaps those blocks triggers a guaranteed panic.

---

### Finding Description

**Root cause — the `.expect()` call:**

In `rpc/src/util/fee_rate.rs` the closure passed to `collect` destructures every `BlockExt` and immediately panics if `txs_sizes` is absent:

```rust
let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
``` [1](#0-0) 

There is no guard, no `if let Some(...)`, and no early-return before this line.

**How `txs_sizes` becomes `None` in storage:**

`get_block_ext` in `store/src/store.rs` reads the raw bytes and branches on the field count. When `count_extra_fields() == 0` (the pre-v0.106 five-field `BlockExt` schema), it converts via `packed::BlockExtReader::into()`: [2](#0-1) 

That conversion path explicitly sets both new fields to `None`:

```rust
cycles: None,
txs_sizes: None,
``` [3](#0-2) 

The schema difference is explicit in the molecule definitions — `BlockExt` (5 fields) vs `BlockExtV1` (7 fields, adding `cycles` and `txs_sizes`): [4](#0-3) 

**Why `filter_map` does not protect against this:**

`collect` uses `filter_map` only to skip blocks whose hash or ext record is entirely absent from storage. A block stored in the old format is still returned as `Some(BlockExt { txs_sizes: None, … })` and is passed directly into the panicking closure: [5](#0-4) 

**A second production path — `Switch::DISABLE_ALL`:**

In `reconcile_main_chain`, when `switch.disable_all()` is true, `insert_ok_ext` is called with `txs_sizes: None`, writing a `BlockExtV1` record whose `txs_sizes` option is empty: [6](#0-5) [7](#0-6) 

**All new writes use `BlockExtV1`**, so a fresh node syncing from genesis with the current code and normal verification will populate `txs_sizes`. The vulnerability is specific to nodes carrying pre-v0.106 data or blocks committed under `DISABLE_ALL`. [8](#0-7) 

---

### Impact Explanation

A panic in the RPC handler crashes the request and, depending on the RPC server's panic-handling strategy, may crash the entire node process. At minimum it is a reliable, repeatable denial-of-service against the `get_fee_rate_statistics` endpoint on any upgraded mainnet node. Impact is scoped to local RPC API crash (0–500).

---

### Likelihood Explanation

Any mainnet node that was running before v0.106 and upgraded in-place retains old-format `BlockExt` records for all blocks committed before the upgrade. The maximum window is 101 blocks (`MAX_TARGET`), so on a node with even a single pre-migration block in the last 101 blocks the panic is guaranteed. The RPC requires no authentication. [9](#0-8) 

---

### Recommendation

Replace the `.expect()` with a graceful skip:

```rust
let Some(txs_sizes) = txs_sizes else {
    return fee_rates; // skip blocks without size data
};
```

This matches the existing guard `if txs_sizes.len() > 1 && !txs_fees.is_empty()` that already handles the case where there is nothing useful to compute. [10](#0-9) 

---

### Proof of Concept

1. Start a node that was previously running pre-v0.106 (or manually insert a `BlockExt` record with the old 5-field schema for any block in the last 101 blocks).
2. Call `get_fee_rate_statistics` with `target = 101` (or any value whose window overlaps the old-format block).
3. The node panics with `"expect txs_size's length >= 1"` at `rpc/src/util/fee_rate.rs:93`.

### Citations

**File:** rpc/src/util/fee_rate.rs (L6-8)
```rust
const DEFAULT_TARGET: u64 = 21;
const MIN_TARGET: u64 = 1;
const MAX_TARGET: u64 = 101;
```

**File:** rpc/src/util/fee_rate.rs (L45-47)
```rust
        let block_ext_iter =
            (start..=tip_number).filter_map(|number| self.get_block_ext_by_number(number));
        block_ext_iter.fold(Vec::new(), f)
```

**File:** rpc/src/util/fee_rate.rs (L87-111)
```rust
            let BlockExt {
                txs_sizes,
                cycles,
                txs_fees,
                ..
            } = block_ext;
            let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
            if txs_sizes.len() > 1 && !txs_fees.is_empty() {
                // block_ext.txs_fees's length == block_ext.cycles's length
                // block_ext.txs_fees's length + 1 == txs_sizes's length
                if let Some(cycles) = cycles {
                    for (fee, cycles, size) in itertools::izip!(
                        txs_fees,
                        cycles,
                        txs_sizes.iter().skip(1) // skip cellbase (first element in the Vec)
                    ) {
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
                        }
                    }
                }
            }
            fee_rates
        });
```

**File:** store/src/store.rs (L247-263)
```rust
    fn get_block_ext(&self, block_hash: &packed::Byte32) -> Option<BlockExt> {
        self.get(COLUMN_BLOCK_EXT, block_hash.as_slice())
            .map(|slice| {
                let reader =
                    packed::BlockExtReader::from_compatible_slice_should_be_ok(slice.as_ref());
                match reader.count_extra_fields() {
                    0 => reader.into(),
                    2 => packed::BlockExtV1Reader::from_slice_should_be_ok(slice.as_ref()).into(),
                    _ => {
                        panic!(
                            "BlockExt storage field count doesn't match, expect 7 or 5, actual {}",
                            reader.field_count()
                        )
                    }
                }
            })
    }
```

**File:** util/types/src/conversion/storage.rs (L139-151)
```rust
impl<'r> Unpack<core::BlockExt> for packed::BlockExtReader<'r> {
    fn unpack(&self) -> core::BlockExt {
        core::BlockExt {
            received_at: self.received_at().into(),
            total_difficulty: self.total_difficulty().into(),
            total_uncles_count: self.total_uncles_count().into(),
            verified: self.verified().into(),
            txs_fees: self.txs_fees().into(),
            cycles: None,
            txs_sizes: None,
        }
    }
}
```

**File:** util/gen-types/schemas/extensions.mol (L66-82)
```text
table BlockExt {
    total_difficulty:   Uint256,
    total_uncles_count: Uint64,
    received_at:        Uint64,
    txs_fees:           Uint64Vec,
    verified:           BoolOpt,
}

table BlockExtV1 {
    total_difficulty:   Uint256,
    total_uncles_count: Uint64,
    received_at:        Uint64,
    txs_fees:           Uint64Vec,
    verified:           BoolOpt,
    cycles:             Uint64VecOpt,
    txs_sizes:          Uint64VecOpt,
}
```

**File:** chain/src/verify.rs (L718-724)
```rust
            } else {
                txn.attach_block(b)?;
                attach_block_cell(&txn, b)?;
                mmr.push(b.digest())
                    .map_err(|e| InternalErrorKind::MMR.other(e))?;
                self.insert_ok_ext(&txn, &b.header().hash(), ext.clone(), None, None)?;
            }
```

**File:** chain/src/verify.rs (L758-776)
```rust
    fn insert_ok_ext(
        &self,
        txn: &StoreTransaction,
        hash: &Byte32,
        mut ext: BlockExt,
        cache_entries: Option<&[Completed]>,
        txs_sizes: Option<Vec<u64>>,
    ) -> Result<(), Error> {
        ext.verified = Some(true);
        if let Some(entries) = cache_entries {
            let (txs_fees, cycles) = entries
                .iter()
                .map(|entry| (entry.fee, entry.cycles))
                .unzip();
            ext.txs_fees = txs_fees;
            ext.cycles = Some(cycles);
        }
        ext.txs_sizes = txs_sizes;
        txn.insert_block_ext(hash, &ext)
```

**File:** store/src/transaction.rs (L241-252)
```rust
    pub fn insert_block_ext(
        &self,
        block_hash: &packed::Byte32,
        ext: &BlockExt,
    ) -> Result<(), Error> {
        let packed_ext: packed::BlockExtV1 = ext.into();
        self.insert_raw(
            COLUMN_BLOCK_EXT,
            block_hash.as_slice(),
            packed_ext.as_slice(),
        )
    }
```
