The call chain is fully traceable. Let me confirm the exact conversion path for old-format `BlockExt` records.

### Title
Unconditional `expect()` on `txs_sizes` in `FeeRateCollector::statistics` panics on pre-v0.106 `BlockExt` records — (`rpc/src/util/fee_rate.rs`)

---

### Summary

An unprivileged RPC caller can trigger a Rust panic inside `get_fee_rate_statistics` (or its deprecated alias `get_fee_rate_statics`) by sending any request to a node whose `COLUMN_BLOCK_EXT` database still contains old 5-field `BlockExt` records written before v0.106. The closure passed to `collect()` calls `txs_sizes.expect(…)` unconditionally; for every pre-v0.106 block the deserialized `core::BlockExt` has `txs_sizes: None`, so the `expect` fires.

---

### Finding Description

**Deserialization path — old format yields `txs_sizes: None`**

`get_block_ext` in `store/src/store.rs` dispatches on `count_extra_fields()`:

```
count_extra_fields == 0  →  packed::BlockExtReader::into()
count_extra_fields == 2  →  packed::BlockExtV1Reader::into()
```

The `From<packed::BlockExtReader>` impl (both `Unpack` and `From` variants) hard-codes:

```rust
cycles: None,
txs_sizes: None,
``` [1](#0-0) 

So every block written before v0.106 returns `Some(core::BlockExt { txs_sizes: None, … })` from `get_block_ext_by_number`.

**`collect()` does not filter `txs_sizes: None`**

```rust
let block_ext_iter =
    (start..=tip_number).filter_map(|number| self.get_block_ext_by_number(number));
block_ext_iter.fold(Vec::new(), f)
``` [2](#0-1) 

`filter_map` only drops blocks for which `get_block_ext_by_number` returns `None` (i.e., the block hash is missing). Old-format blocks return `Some(…)` and are forwarded to the closure.

**Unconditional `expect` in the closure**

```rust
let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
``` [3](#0-2) 

There is no guard, no `if let Some`, no early `continue`. For every pre-v0.106 block this line panics.

**Dispatch in `get_block_ext`**

```rust
match reader.count_extra_fields() {
    0 => reader.into(),          // old 5-field → txs_sizes: None
    2 => packed::BlockExtV1Reader::from_slice_should_be_ok(slice.as_ref()).into(),
    _ => { panic!(…) }
}
``` [4](#0-3) 

**No `catch_unwind` in the RPC layer**

A search of `rpc/**/*.rs` finds zero uses of `catch_unwind`, `PanicHandler`, or equivalent. The panic propagates unguarded through the synchronous handler into the tokio worker thread.

---

### Impact Explanation

A panic in a tokio worker thread that is not caught by `catch_unwind` causes that thread to unwind and the task to abort. Depending on the tokio runtime configuration and the jsonrpc-core version in use, this either:

- aborts only the RPC task (request returns an internal error, but the node survives), or
- propagates to the thread pool and terminates the node process.

Either outcome is triggered by a single unauthenticated HTTP POST to the public JSON-RPC port. The attacker does not need any key, peer relationship, or PoW. Repeated calls keep the node in a degraded or crashed state.

---

### Likelihood Explanation

Any mainnet or testnet node that was running before v0.106 and upgraded in-place (without a full re-sync) retains old-format `BlockExt` records for every block written before the upgrade. The `target` parameter is capped at 101, so the window always includes those old blocks as long as the chain tip is within 101 blocks of the upgrade height — or if the node was restarted and the old records were never migrated. No special knowledge is required; the attacker only needs to know the node's RPC endpoint.

---

### Recommendation

Replace the unconditional `expect` with a graceful skip:

```rust
let Some(txs_sizes) = txs_sizes else {
    return fee_rates; // skip pre-v0.106 blocks silently
};
```

Alternatively, filter in `collect()` or add a migration that back-fills `txs_sizes` for old blocks during node startup.

---

### Proof of Concept

1. Start a CKB node that has `COLUMN_BLOCK_EXT` entries with 5-field `BlockExt` (i.e., any node upgraded from before v0.106 without re-sync).
2. Send:
   ```json
   {"jsonrpc":"2.0","method":"get_fee_rate_statistics","params":[{"value":"0x65"}],"id":1}
   ```
3. `collect(101, …)` iterates the last 101 blocks, hits the first old-format block, deserializes it to `core::BlockExt { txs_sizes: None, … }`, and the closure fires `None.expect("expect txs_size's length >= 1")` → **panic**.

The bug is directly reproducible with a unit test by inserting a `BlockExt { txs_sizes: None, … }` into a `DummyFeeRateProvider` and calling `FeeRateCollector::new(&provider).statistics(None)` — the existing test suite never exercises this case because every test entry sets `txs_sizes: Some(…)`. [5](#0-4)

### Citations

**File:** util/types/src/conversion/storage.rs (L154-165)
```rust
impl<'r> From<packed::BlockExtReader<'r>> for core::BlockExt {
    fn from(value: packed::BlockExtReader<'r>) -> core::BlockExt {
        core::BlockExt {
            received_at: value.received_at().into(),
            total_difficulty: value.total_difficulty().into(),
            total_uncles_count: value.total_uncles_count().into(),
            verified: value.verified().into(),
            txs_fees: value.txs_fees().into(),
            cycles: None,
            txs_sizes: None,
        }
    }
```

**File:** rpc/src/util/fee_rate.rs (L45-47)
```rust
        let block_ext_iter =
            (start..=tip_number).filter_map(|number| self.get_block_ext_by_number(number));
        block_ext_iter.fold(Vec::new(), f)
```

**File:** rpc/src/util/fee_rate.rs (L93-93)
```rust
            let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
```

**File:** store/src/store.rs (L252-261)
```rust
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
```

**File:** rpc/src/tests/fee_rate.rs (L51-65)
```rust
        let ext = BlockExt {
            received_at: 0,
            total_difficulty: 0u64.into(),
            total_uncles_count: 0,
            verified: None,

            // txs_fees length is equal to block_ext.cycles length
            // and txs_fees does not include cellbase
            txs_fees: vec![Capacity::shannons(i * i * 100)],
            // cycles does not include cellbase
            cycles: Some(vec![i * 100]),
            // txs_sizes length is equal to block_ext.txs_fees length + 1
            // first element in txs_sizes is belong to cellbase
            txs_sizes: Some(vec![i * 5678, i * 100]),
        };
```
