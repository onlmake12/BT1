### Title
`get_latest_built_filter_block_number` Uses Global Latest Filter Hash Incorrectly After Reorg — (`sync/src/types/mod.rs`)

---

### Summary

`ActiveChain::get_latest_built_filter_block_number()` reads the **global** latest-built-filter block hash from the `META_LATEST_BUILT_FILTER_DATA_KEY` meta column and then calls `get_block_number()` on it. `get_block_number()` only resolves hashes that are in the **main-chain index**. After any chain reorganization the stored hash belongs to the old fork; `get_block_number()` returns `None`, and the function silently falls back to `0`. Every P2P block-filter handler that gates on this value then treats the node as having built zero filters and ignores all incoming requests from light-client peers.

---

### Finding Description

`get_latest_built_filter_block_number` in `sync/src/types/mod.rs`:

```rust
pub fn get_latest_built_filter_block_number(&self) -> BlockNumber {
    self.snapshot
        .get_latest_built_filter_data_block_hash()   // global latest marker
        .and_then(|hash| self.snapshot.get_block_number(&hash))  // main-chain only
        .unwrap_or_default()   // silently returns 0 on fork hash
}
``` [1](#0-0) 

`get_block_number` queries `COLUMN_INDEX`, which only contains main-chain block hashes. After a reorg the hash stored in `META_LATEST_BUILT_FILTER_DATA_KEY` belongs to the detached fork; the lookup returns `None`, and `unwrap_or_default()` produces `0`. [2](#0-1) 

This value is consumed by all three P2P block-filter message handlers:

```rust
// GetBlockFiltersProcess, GetBlockFilterHashesProcess, GetBlockFilterCheckPointsProcess
let latest: BlockNumber = active_chain.get_latest_built_filter_block_number();
if latest >= start_number {
    // serve the request
} else {
    Status::ignored()   // ← silently drops the request
}
``` [3](#0-2) [4](#0-3) [5](#0-4) 

When `latest == 0` and `start_number > 0` (the normal case for any syncing light client), the condition is false and the request is silently dropped.

By contrast, `build_filter_data` in `block-filter/src/filter.rs` **correctly** handles the fork case: it detects that the stored hash is off the main chain and walks back to the fork point before rebuilding. [6](#0-5) 

`get_latest_built_filter_block_number` never performs this backward scan; it just returns `0`.

---

### Impact Explanation

Any light-client peer that sends `GetBlockFilters`, `GetBlockFilterHashes`, or `GetBlockFilterCheckPoints` with `start_number > 0` receives no response (the server returns `Status::ignored()`). The light client cannot distinguish this from a slow peer and will either stall or disconnect. The outage persists until `build_filter_data` finishes rebuilding filters for the new main chain and overwrites `META_LATEST_BUILT_FILTER_DATA_KEY` — a process that is proportional to the depth of the reorg. For a deep reorg this window can span many seconds to minutes, during which the node's entire block-filter service is effectively offline for all light-client peers.

---

### Likelihood Explanation

Chain reorganizations are a normal, externally-triggerable event: any unprivileged block relayer can submit a valid competing chain via the P2P relay protocol. Shallow reorgs (1–2 blocks) occur naturally on mainnet. A moderately resourced attacker can deliberately induce deeper reorgs to extend the outage window. No privileged access, key material, or majority hashpower is required to trigger the condition; a single valid competing block is sufficient to flip the stored hash to a fork hash and drive `get_latest_built_filter_block_number` to return `0`.

---

### Recommendation

Mirror the backward-scan logic already present in `build_filter_data`. When `get_block_number(&hash)` returns `None` (indicating the stored hash is on a fork), walk the stored header's parent chain until a hash that `is_main_chain()` is found, then return that block's number. This is exactly the pattern used in `build_filter_data` at lines 91–104 of `block-filter/src/filter.rs`. [7](#0-6) 

---

### Proof of Concept

1. Node A is at tip height H with `META_LATEST_BUILT_FILTER_DATA_KEY` = hash(H).
2. A block relayer submits a valid competing chain that causes a reorg; the new tip is H′ on a different branch.
3. `META_LATEST_BUILT_FILTER_DATA_KEY` still holds hash(H) (the old fork block).
4. A light-client peer sends `GetBlockFilters { start_number: 1 }`.
5. `get_latest_built_filter_block_number()` calls `get_block_number(hash(H))` → `None` (fork block not in main-chain index) → returns `0`.
6. `0 >= 1` is false → `Status::ignored()` → no response sent to the light client.
7. The light client receives nothing and cannot sync block filters until `build_filter_data` completes the rebuild and updates the meta key.

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

**File:** store/src/store.rs (L480-483)
```rust
    fn get_latest_built_filter_data_block_hash(&self) -> Option<packed::Byte32> {
        self.get(COLUMN_META, META_LATEST_BUILT_FILTER_DATA_KEY)
            .map(|raw| packed::Byte32Reader::from_slice_should_be_ok(raw.as_ref()).to_entity())
    }
```

**File:** sync/src/filter/get_block_filters_process.rs (L33-38)
```rust
    pub async fn execute(self) -> Status {
        let active_chain = self.filter.shared.active_chain();
        let start_number: BlockNumber = self.message.to_entity().start_number().into();
        let latest: BlockNumber = active_chain.get_latest_built_filter_block_number();

        if latest >= start_number {
```

**File:** sync/src/filter/get_block_filter_hashes_process.rs (L33-39)
```rust
        let active_chain = self.filter.shared.active_chain();
        let start_number: BlockNumber = self.message.to_entity().start_number().into();
        let latest: BlockNumber = active_chain.get_latest_built_filter_block_number();

        let mut block_filter_hashes = Vec::new();

        if latest >= start_number {
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L34-41)
```rust
    pub async fn execute(self) -> Status {
        let active_chain = self.filter.shared.active_chain();
        let start_number: BlockNumber = self.message.to_entity().start_number().into();
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
