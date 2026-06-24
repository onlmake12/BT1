Audit Report

## Title
`AddBlockFilterHash::migrate` Panics on Startup After Reorg When Fork-Chain Filter Data Exists Without Main-Chain Counterpart — (`util/migrate/src/migrations/add_block_filter_hash.rs`)

## Summary

The fork-chain walk in `AddBlockFilterHash::migrate` terminates with `header` pointing to the first diverging fork block at height K, and returns `header.number()` (= K) as `latest_built_filter_data_block_number`. The subsequent migration loop then calls `get_block_filter` on the **main-chain** block hash at height K — which has no filter data when the node shut down before the filter service rebuilt that block after a reorg — causing an unconditional panic via `.expect("filter data stored")`. Any node that had block filter enabled, experienced a reorg, and shut down before the filter service wrote the new main-chain block at the fork height will be permanently unable to start after upgrading to v0.108+.

## Finding Description

**Fork-chain walk returns K (fork block height), not K-1 (last shared ancestor).**

In `util/migrate/src/migrations/add_block_filter_hash.rs` lines 29–37:

```rust
let mut header = chain_db.get_block_header(&block_hash).expect("header stored");
while !chain_db.is_main_chain(&header.parent_hash()) {
    header = chain_db.get_block_header(&header.parent_hash()).expect("parent header stored");
}
header.number()   // ← K: the first fork block, NOT K-1 (the last shared ancestor)
``` [1](#0-0) 

The loop exits when `header.parent_hash()` is on the main chain, meaning `header` itself is the first block that diverged (height K). Its parent at K-1 is the last shared ancestor. `header.number()` = K is therefore one too high.

**`is_main_chain` is keyed on block hash in `COLUMN_INDEX`.** [2](#0-1) 

After a reorg, `COLUMN_INDEX[number=K]` maps to the **new main-chain** block hash at K, not the old fork block hash. The fork block hash at K has no `COLUMN_INDEX` entry, so `is_main_chain(fork_hash_K)` = false, confirming the walk correctly identifies the fork block — but then returns its height K instead of K-1.

**Migration loop reads main-chain filter data at height K.** [3](#0-2) 

`get_block_hash(K)` returns the main-chain block hash at K. `get_block_filter` reads `COLUMN_BLOCK_FILTER` keyed by that hash: [4](#0-3) 

`insert_block_filter` writes `COLUMN_BLOCK_FILTER` and `META_LATEST_BUILT_FILTER_DATA_KEY` atomically per block: [5](#0-4) 

If the node shuts down after the reorg is committed to `COLUMN_INDEX` but before `build_filter_data_for_block` writes the new main-chain block at K, then:
- `META_LATEST_BUILT_FILTER_DATA_KEY` → fork block hash at height H (H ≥ K)
- `COLUMN_BLOCK_FILTER[fork_hash_K]` → valid filter bytes
- `COLUMN_BLOCK_FILTER[main_chain_hash_K]` → **absent**

`get_block_filter(main_chain_hash_K)` returns `None`; `.expect("filter data stored")` panics unconditionally.

**The `build_filter_data_for_block` guard in `block-filter/src/filter.rs` does not protect the migration.** [6](#0-5) 

This guard checks `COLUMN_BLOCK_FILTER_HASH` (not `COLUMN_BLOCK_FILTER`) and only runs during the live filter service, not during the migration. The migration has no equivalent guard and panics instead.

## Impact Explanation

The node process terminates with a panic during the mandatory `AddBlockFilterHash` migration step on every startup attempt. The node cannot start at all. Recovery requires manual RocksDB surgery or a full re-sync. This matches **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node** — the crash is permanent (not transient) and affects every node meeting the preconditions.

## Likelihood Explanation

All three preconditions are routine operational events on a PoW chain: block filter is commonly enabled on nodes serving light clients (v0.105+), reorgs occur naturally, and the window between a reorg being committed to `COLUMN_INDEX` and the filter service writing the first new main-chain block is non-zero — any SIGTERM, OOM kill, power loss, or graceful shutdown during that window produces the vulnerable DB state. No attacker action is required; the state arises from normal node operation. Every such node is permanently broken on upgrade to v0.108+.

## Recommendation

Change the fork-chain walk upper bound from `header.number()` (K, the fork block height) to `header.number().saturating_sub(1)` (K-1, the last shared ancestor), so the migration loop only covers blocks whose main-chain filter data is guaranteed to exist:

```rust
// Before (buggy): returns K — the fork block height
header.number()

// After (correct): returns K-1 — the last common ancestor
header.number().saturating_sub(1)
```

The identical walk in `block-filter/src/filter.rs` lines 92–104 also returns `header.number()` as `start_number` for rebuilding, which is intentionally correct there (it needs to rebuild from K on the new main chain). The migration's use of the same value as an **upper bound** for already-built data is the bug. [7](#0-6) 

## Proof of Concept

1. Construct a RocksDB fixture with:
   - `COLUMN_META[META_LATEST_BUILT_FILTER_DATA_KEY]` = `fork_block_hash_K` (a hash not in `COLUMN_INDEX`)
   - `COLUMN_BLOCK_HEADER[fork_block_hash_K]` = valid header at height K, parent = `main_chain_hash_{K-1}`
   - `COLUMN_INDEX[main_chain_hash_{K-1}]` = `K-1` (parent is on main chain)
   - `COLUMN_INDEX[K]` = `main_chain_hash_K` (different from `fork_block_hash_K`)
   - `COLUMN_BLOCK_FILTER[fork_block_hash_K]` = valid filter bytes
   - `COLUMN_BLOCK_FILTER[main_chain_hash_K]` = **absent**
   - `COLUMN_BLOCK_FILTER` entries for blocks 0 through K-1 on the main chain = present
2. Run `AddBlockFilterHash::migrate` against this DB.
3. Observe panic: `thread 'main' panicked at 'filter data stored'` when `block_number == K`, because `get_block_hash(K)` returns `main_chain_hash_K` and `get_block_filter(main_chain_hash_K)` returns `None`.

### Citations

**File:** util/migrate/src/migrations/add_block_filter_hash.rs (L29-37)
```rust
                let mut header = chain_db
                    .get_block_header(&block_hash)
                    .expect("header stored");
                while !chain_db.is_main_chain(&header.parent_hash()) {
                    header = chain_db
                        .get_block_header(&header.parent_hash())
                        .expect("parent header stored");
                }
                header.number()
```

**File:** util/migrate/src/migrations/add_block_filter_hash.rs (L61-64)
```rust
                    let block_hash = chain_db.get_block_hash(block_number).expect("index stored");
                    let filter_data = chain_db
                        .get_block_filter(&block_hash)
                        .expect("filter data stored");
```

**File:** store/src/store.rs (L279-281)
```rust
    fn is_main_chain(&self, hash: &packed::Byte32) -> bool {
        self.get(COLUMN_INDEX, hash.as_slice()).is_some()
    }
```

**File:** store/src/store.rs (L486-489)
```rust
    fn get_block_filter(&self, hash: &packed::Byte32) -> Option<packed::Bytes> {
        self.get(COLUMN_BLOCK_FILTER, hash.as_slice())
            .map(|slice| packed::BytesReader::from_slice_should_be_ok(slice.as_ref()).to_entity())
    }
```

**File:** store/src/transaction.rs (L392-414)
```rust
    pub fn insert_block_filter(
        &self,
        block_hash: &packed::Byte32,
        filter_data: &packed::Bytes,
        parent_block_filter_hash: &packed::Byte32,
    ) -> Result<(), Error> {
        self.insert_raw(
            COLUMN_BLOCK_FILTER,
            block_hash.as_slice(),
            filter_data.as_slice(),
        )?;
        let current_block_filter_hash = calc_filter_hash(parent_block_filter_hash, filter_data);
        self.insert_raw(
            COLUMN_BLOCK_FILTER_HASH,
            block_hash.as_slice(),
            current_block_filter_hash.as_slice(),
        )?;
        self.insert_raw(
            COLUMN_META,
            META_LATEST_BUILT_FILTER_DATA_KEY,
            block_hash.as_slice(),
        )
    }
```

**File:** block-filter/src/filter.rs (L91-104)
```rust
                    // find fork chain number
                    let mut header = snapshot
                        .get_block_header(&block_hash)
                        .expect("header stored");
                    while !snapshot.is_main_chain(&header.parent_hash()) {
                        header = snapshot
                            .get_block_header(&header.parent_hash())
                            .expect("parent header stored");
                    }
                    debug!(
                        "Block with the latest built filter data on the forked chain, starting from {}",
                        header.number()
                    );
                    header.number()
```

**File:** block-filter/src/filter.rs (L131-137)
```rust
        if db.get_block_filter_hash(&header.hash()).is_some() {
            debug!(
                "Filter data for block {:#x} already exists. Skip building.",
                header.hash()
            );
            return;
        }
```
