### Title
Unbounded Growth of `block_status_map` via Unremoved `BLOCK_INVALID` Entries — (`shared/src/shared.rs`)

---

### Summary

The in-memory `block_status_map` (`DashMap<Byte32, BlockStatus>`) in `shared/src/shared.rs` has no size cap and no eviction policy for `BLOCK_INVALID` entries. Every invalid block or header received from a remote peer causes a permanent insertion into this map. Because `remove_block_status()` is never called for `BLOCK_INVALID` entries, the map grows monotonically for the lifetime of the node process. An unprivileged peer acting as a block/header relayer can exploit this to exhaust node memory and cause a Denial-of-Service.

---

### Finding Description

`block_status_map` is declared as a `DashMap<Byte32, BlockStatus>` with no capacity bound: [1](#0-0) 

Entries are inserted via `insert_block_status()`: [2](#0-1) 

The removal counterpart, `remove_block_status()`, calls `shrink_to_fit` after deletion: [3](#0-2) 

However, `remove_block_status()` is **only** invoked on the success path (valid blocks that complete processing). For blocks that fail non-contextual verification, `BLOCK_INVALID` is inserted and the function returns immediately — no corresponding removal ever occurs: [4](#0-3) 

The same pattern appears in header validation: every header that fails `prev_block_check`, `non_contextual_check`, or `version_check` inserts a permanent `BLOCK_INVALID` entry: [5](#0-4) 

The compact-block relay path also checks this map and returns `StatusCode::BlockIsInvalid` without removing the entry, confirming the entry persists indefinitely: [6](#0-5) 

The CKB project's own CHANGELOG acknowledged a related unbounded-growth scenario for `block_status_map` during IBD (fixed in v0.34.0 for the DB-fetch path), but the accumulation of `BLOCK_INVALID` entries from peer-supplied invalid blocks was not addressed: [7](#0-6) 

---

### Impact Explanation

The `block_status_map` resides entirely in process memory. Because `BLOCK_INVALID` entries are never evicted, the map grows proportionally to the number of unique invalid block hashes the node has ever seen. Each `Byte32` key is 32 bytes; with associated `BlockStatus` metadata and `DashMap` overhead, each entry consumes on the order of ~100–200 bytes. Sending 10 million unique invalid block hashes would consume roughly 1–2 GB of heap memory. At sufficient scale this causes OOM termination of the node process, a complete Denial-of-Service.

---

### Likelihood Explanation

A remote peer acting as a compact-block or header relayer is the attacker. The attacker crafts blocks with unique hashes (by varying nonce, timestamp, or transaction set) and submits them via the sync or relay protocol. Each unique invalid block hash that passes the initial `block_status_map` lookup (i.e., is not already known) triggers `insert_block_status(hash, BLOCK_INVALID)`.

Whether the peer is immediately banned after one invalid submission affects attack speed but not feasibility:
- If the peer is disconnected-but-not-banned, the attacker reconnects and repeats, accumulating entries across sessions.
- If the peer is banned by IP, the attacker rotates source addresses (a low-cost operation on cloud infrastructure).

The node has no size limit, no LRU eviction, and no periodic cleanup for `BLOCK_INVALID` entries, so every successful submission is permanent. The attack is slow but requires no special privilege and no cryptographic capability.

---

### Recommendation

1. **Cap the map size**: Enforce a maximum entry count for `block_status_map` (e.g., 1 million entries). When the cap is reached, evict the oldest or least-recently-used `BLOCK_INVALID` entries.
2. **Periodic cleanup**: Add a background task that removes `BLOCK_INVALID` entries older than a configurable TTL (e.g., 24 hours), since re-checking a known-invalid block against the database is cheap.
3. **Peer rate-limiting**: Enforce a per-peer limit on the number of invalid blocks accepted per time window before banning, reducing the rate at which entries can be injected.

---

### Proof of Concept

1. Establish a sync protocol connection to a CKB node.
2. In a loop, construct `SendHeaders` messages each containing a header with a unique hash that fails `non_contextual_check` (e.g., wrong version field, invalid parent hash, or invalid PoW).
3. Send each message. Observe via `/proc/<pid>/status` (`VmRSS`) that the node's resident memory grows monotonically.
4. After sufficient iterations (scale depends on available attacker bandwidth), the node process is killed by the OS OOM killer or becomes unresponsive due to memory pressure.

The relevant insertion site triggered by each such header: [8](#0-7) 

The map that accumulates these entries with no bound: [9](#0-8)

### Citations

**File:** shared/src/shared.rs (L421-457)
```rust
    pub fn block_status_map(&self) -> &DashMap<Byte32, BlockStatus> {
        &self.block_status_map
    }

    pub fn get_block_status(&self, block_hash: &Byte32) -> BlockStatus {
        match self.block_status_map().get(block_hash) {
            Some(status_ref) => *status_ref.value(),
            None => {
                if self.header_map().contains_key(block_hash) {
                    BlockStatus::HEADER_VALID
                } else {
                    let verified = self
                        .snapshot()
                        .get_block_ext(block_hash)
                        .map(|block_ext| block_ext.verified);
                    match verified {
                        None => BlockStatus::UNKNOWN,
                        Some(None) => BlockStatus::BLOCK_STORED,
                        Some(Some(true)) => BlockStatus::BLOCK_VALID,
                        Some(Some(false)) => BlockStatus::BLOCK_INVALID,
                    }
                }
            }
        }
    }

    pub fn contains_block_status<T: ChainStore>(
        &self,
        block_hash: &Byte32,
        status: BlockStatus,
    ) -> bool {
        self.get_block_status(block_hash).contains(status)
    }

    pub fn insert_block_status(&self, block_hash: Byte32, status: BlockStatus) {
        self.block_status_map.insert(block_hash, status);
    }
```

**File:** shared/src/shared.rs (L459-468)
```rust
    pub fn remove_block_status(&self, block_hash: &Byte32) {
        let log_now = std::time::Instant::now();
        self.block_status_map.remove(block_hash);
        debug!("remove_block_status cost {:?}", log_now.elapsed());
        shrink_to_fit!(self.block_status_map, SHRINK_THRESHOLD);
        debug!(
            "remove_block_status shrink_to_fit cost {:?}",
            log_now.elapsed()
        );
    }
```

**File:** chain/src/chain_service.rs (L120-130)
```rust
            let result = self.non_contextual_verify(lonely_block.block());
            if let Err(err) = result {
                error!(
                    "block {}-{} verify failed: {:?}",
                    block_number, block_hash, err
                );
                self.shared
                    .insert_block_status(lonely_block.block().hash(), BlockStatus::BLOCK_INVALID);
                lonely_block.execute_callback(Err(err));
                return;
            }
```

**File:** sync/src/synchronizer/headers_process.rs (L324-354)
```rust
        if self.prev_block_check(&mut result).is_err() {
            debug!(
                "HeadersProcess rejected invalid-parent header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            return result;
        }

        if let Some(is_invalid) = self.non_contextual_check(&mut result).err() {
            debug!(
                "HeadersProcess rejected non-contextual header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            if is_invalid {
                shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            }
            return result;
        }

        if self.version_check(&mut result).is_err() {
            debug!(
                "HeadersProcess rejected invalid-version header: {} {}",
                self.header.number(),
                self.header.hash(),
            );
            shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID);
            return result;
        }
```

**File:** sync/src/relayer/tests/compact_block_process.rs (L60-71)
```rust
    // BLOCK_INVALID in block_status_map
    {
        relayer
            .shared
            .shared()
            .insert_block_status(block.header().hash(), BlockStatus::BLOCK_INVALID);
    }

    assert_eq!(
        rt.block_on(compact_block_process.execute()),
        StatusCode::BlockIsInvalid.into(),
    );
```

**File:** CHANGELOG.md (L1364-1370)
```markdown
- #2145: Don't cache all block status in memory (@yangby-cryptape)

    When a node is start from number 0, it will sync all headers at first, then the `block_status_map` will be full quickly.
    Then, along with the block sync, all data in `block_status_map` will be removed.
    When the IBD is done, there will be nothing left in `block_status_map`.

    But when another new-started node try to sync data from this node, this node will fetch all block statuses from database and insert them into `block_status_map` without deletions. And full block status will store in memory until the node is shutdown.
```
