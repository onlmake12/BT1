Audit Report

## Title
`get_latest_built_filter_block_number` Returns 0 After Reorg, Silently Dropping All Light-Client Filter Requests — (`sync/src/types/mod.rs`)

## Summary
`ActiveChain::get_latest_built_filter_block_number()` reads the globally stored latest-filter block hash from `META_LATEST_BUILT_FILTER_DATA_KEY` and resolves it via `get_block_number()`, which only queries the main-chain index (`COLUMN_INDEX`). After any chain reorganization the stored hash belongs to the detached fork; `get_block_number()` returns `None` and `unwrap_or_default()` silently returns `0`. All three P2P block-filter handlers gate on this value and return `Status::ignored()` for any `start_number > 0`, making the node's entire block-filter service unresponsive to light-client peers until `build_filter_data` finishes rebuilding and overwrites the meta key.

## Finding Description

**Root cause — `get_latest_built_filter_block_number`:**

```rust
// sync/src/types/mod.rs L1672-1677
pub fn get_latest_built_filter_block_number(&self) -> BlockNumber {
    self.snapshot
        .get_latest_built_filter_data_block_hash()   // reads META_LATEST_BUILT_FILTER_DATA_KEY
        .and_then(|hash| self.snapshot.get_block_number(&hash))  // COLUMN_INDEX: main-chain only
        .unwrap_or_default()   // returns 0 when hash is on a fork
}
``` [1](#0-0) 

`get_latest_built_filter_data_block_hash()` reads from `COLUMN_META` using `META_LATEST_BUILT_FILTER_DATA_KEY`. [2](#0-1) 

`get_block_number()` resolves only hashes present in `COLUMN_INDEX`, which is the main-chain block index. A hash belonging to a detached fork block is absent from this column, so the call returns `None`.

After a reorg, `META_LATEST_BUILT_FILTER_DATA_KEY` still holds the hash of the last block for which filters were built on the old chain. That hash is no longer in `COLUMN_INDEX`, so `get_block_number` returns `None` → `unwrap_or_default()` → `0`.

**Downstream effect — all three P2P filter handlers:**

All three handlers read this value and gate on `latest >= start_number`: [3](#0-2) [4](#0-3) [5](#0-4) 

When `latest == 0` and `start_number > 0` (the normal case for any syncing light client), the condition is false and `Status::ignored()` is returned — no response is sent.

**Contrast with `build_filter_data` which handles this correctly:**

`build_filter_data` explicitly checks `is_main_chain(&block_hash)` and, when the stored hash is on a fork, walks the parent chain until it finds a main-chain ancestor before resuming filter construction. [6](#0-5) 

`get_latest_built_filter_block_number` performs no such backward scan.

## Impact Explanation

The block-filter service becomes completely unresponsive to all light-client peers for the duration of the reorg recovery window. Light clients sending `GetBlockFilters`, `GetBlockFilterHashes`, or `GetBlockFilterCheckPoints` with any `start_number > 0` receive no reply. This matches **"Suboptimal implementation of CKB state storage mechanism"** (Medium, 2001–10000 points): the state read path for the filter service is logically inconsistent with the write path (`build_filter_data`), causing a functional outage of the light-client filter protocol during a normal network event.

## Likelihood Explanation

Chain reorganizations are a routine, externally-triggerable event requiring no privileged access. Shallow reorgs (1–2 blocks) occur naturally on mainnet. Any unprivileged peer can submit a valid competing block via the P2P relay protocol to trigger the condition. The outage window scales with reorg depth: for a 1-block reorg it is brief; for deeper reorgs it persists until `build_filter_data` completes its rebuild loop, which is proportional to the number of blocks that must be reprocessed.

## Recommendation

Mirror the backward-scan logic already present in `build_filter_data`. In `get_latest_built_filter_block_number`, when `get_block_number(&hash)` returns `None`, retrieve the stored block's header, walk its parent chain via `get_block_header(&header.parent_hash())` until `is_main_chain()` returns true, and return that ancestor's block number. This is exactly the pattern at lines 91–104 of `block-filter/src/filter.rs`. [7](#0-6) 

## Proof of Concept

1. Node is at tip height H; `META_LATEST_BUILT_FILTER_DATA_KEY` = `hash(H)`.
2. A peer submits a valid competing chain causing a reorg; new tip is H′ on a different branch.
3. `META_LATEST_BUILT_FILTER_DATA_KEY` still holds `hash(H)` (the detached fork block).
4. A light-client peer sends `GetBlockFilters { start_number: 1 }`.
5. `get_latest_built_filter_block_number()` calls `get_block_number(hash(H))` → `None` (fork block absent from `COLUMN_INDEX`) → returns `0`.
6. `0 >= 1` is false → `Status::ignored()` → no response sent.
7. Light client receives nothing and cannot sync block filters until `build_filter_data` completes the rebuild and overwrites the meta key.

A unit test can reproduce this by: (a) writing a block hash to `META_LATEST_BUILT_FILTER_DATA_KEY` that is not present in `COLUMN_INDEX`, (b) calling `get_latest_built_filter_block_number()`, and (c) asserting the return value is `0` rather than the correct fork-point ancestor number.

### Citations

**File:** sync/src/types/mod.rs (L1672-1677)
```rust
    pub fn get_latest_built_filter_block_number(&self) -> BlockNumber {
        self.snapshot
            .get_latest_built_filter_data_block_hash()
            .and_then(|hash| self.snapshot.get_block_number(&hash))
            .unwrap_or_default()
    }
```

**File:** store/src/store.rs (L479-483)
```rust
    /// Gets latest built filter data block hash
    fn get_latest_built_filter_data_block_hash(&self) -> Option<packed::Byte32> {
        self.get(COLUMN_META, META_LATEST_BUILT_FILTER_DATA_KEY)
            .map(|raw| packed::Byte32Reader::from_slice_should_be_ok(raw.as_ref()).to_entity())
    }
```

**File:** sync/src/filter/get_block_filters_process.rs (L36-38)
```rust
        let latest: BlockNumber = active_chain.get_latest_built_filter_block_number();

        if latest >= start_number {
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L35-39)
```rust
        let latest: BlockNumber = active_chain.get_latest_built_filter_block_number();

        let mut block_filter_hashes = Vec::new();

        if latest >= start_number {
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L37-41)
```rust
        let latest: BlockNumber = active_chain.get_latest_built_filter_block_number();

        let mut block_filter_hashes = Vec::new();

        if latest >= start_number {
```

**File:** block-filter/src/filter.rs (L78-108)
```rust
        let start_number = match snapshot.get_latest_built_filter_data_block_hash() {
            Some(block_hash) => {
                debug!("Hash of the latest created block {:#x}", block_hash);
                if snapshot.is_main_chain(&block_hash) {
                    let header = snapshot
                        .get_block_header(&block_hash)
                        .expect("header stored");
                    debug!(
                        "Latest created block on the main chain, starting from {}",
                        header.number() + 1
                    );
                    header.number() + 1
                } else {
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
                }
            }
            None => 0,
        };
```
