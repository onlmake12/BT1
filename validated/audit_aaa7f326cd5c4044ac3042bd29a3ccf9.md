### Title
Unbounded `block_status_map` Growth via Unevicted `BLOCK_INVALID` Entries from Header Validation — (`sync/src/synchronizer/headers_process.rs`, `shared/src/shared.rs`)

### Summary

`HeaderAcceptor::accept()` inserts `BLOCK_INVALID` entries into the shared `block_status_map` (`Arc<DashMap<Byte32, BlockStatus>>`) when headers fail `prev_block_check`, `non_contextual_check`, or `version_check`. No code path ever removes these entries. A remote peer can repeatedly reconnect after each 5-minute ban and insert a new unique `BLOCK_INVALID` entry per reconnection cycle, causing the map to grow without bound across the node's lifetime.

---

### Finding Description

In `HeaderAcceptor::accept()`, three failure branches unconditionally call `shared.insert_block_status(self.header.hash(), BlockStatus::BLOCK_INVALID)`: [1](#0-0) 

`insert_block_status` is a bare `DashMap::insert` with no size cap: [2](#0-1) 

`remove_block_status` (the only removal path) is called only in:
- `chain/src/verify.rs` — when a full block is successfully verified or hits an internal DB error
- `chain/src/orphan_broker.rs` — when expired orphan blocks are cleaned up [3](#0-2) [4](#0-3) 

Neither path is ever triggered for entries inserted by `HeaderAcceptor::accept()`, because those headers never proceed to block download or orphan processing. The `BLOCK_INVALID` entries from header validation are permanent residents of the map.

**Concrete attack path:**

1. Attacker connects to the node via P2P sync protocol.
2. Sends a `SendHeaders` message containing a single header whose `parent_hash` is the genesis block hash (known, not `BLOCK_INVALID`) and whose PoW nonce is invalid.
3. `is_continuous` passes (single header). `prev_block_check` passes (genesis is not `BLOCK_INVALID`). `non_contextual_check` calls `HeaderVerifier::verify()`, which fails with a PoW error — not `UnknownParent`, not `too_new` — so `is_invalid = true` and `BLOCK_INVALID` is inserted for this header's unique hash. [5](#0-4) 

4. `HeadersProcess::execute()` returns `StatusCode::HeadersIsInvalid` (a 4xx code).
5. `should_ban()` returns `Some(BAD_MESSAGE_BAN_TIME)` = 5 minutes; the peer is banned. [6](#0-5) 

6. After 5 minutes, the attacker reconnects with a new header (different nonce → different hash) and repeats. Each cycle inserts one new permanent `BLOCK_INVALID` entry.

The `MAX_HEADERS_LEN` cap of 2,000 limits headers per message but does not limit the number of messages over time: [7](#0-6) 

The developers themselves left a `FIXME` comment acknowledging incomplete handling of `BLOCK_INVALID` status in this code path: [8](#0-7) 

---

### Impact Explanation

The `block_status_map` is a process-lifetime `Arc<DashMap<Byte32, BlockStatus>>` with no eviction, no LRU, and no size bound: [9](#0-8) 

Each entry consumes approximately 32 bytes (key) + 4 bytes (value) + DashMap shard overhead (~64–128 bytes). A single attacker IP inserts ~12 entries/hour = ~105,000 entries/year ≈ ~10–15 MB/year. With multiple IPs (e.g., 100), the rate scales linearly to ~1–1.5 GB/year. Over a long-running mainnet node lifetime, this constitutes unbounded RSS growth leading to eventual OOM or severe memory pressure, degrading node availability.

---

### Likelihood Explanation

The attack requires no cryptographic work, no privileged access, and no majority hashpower. Crafting a header with a known parent hash and an invalid PoW nonce is trivial. The only throttle is the 5-minute ban per IP, which is bypassable with multiple IPs or by waiting. The attack is persistent, stealthy (no crash, just slow growth), and requires no coordination beyond a single P2P connection.

---

### Recommendation

1. **Cap the map size**: Enforce a maximum size on `block_status_map` (e.g., LRU eviction or a hard cap with rejection of new `BLOCK_INVALID` inserts when full).
2. **Evict on peer disconnect**: When a peer is banned for sending an invalid header, remove the `BLOCK_INVALID` entry it caused (or avoid inserting it at all — the entry's only purpose is to short-circuit future processing of the same hash, which is already handled by the ban).
3. **Avoid inserting for headers that require PoW work to produce**: Consider not marking headers `BLOCK_INVALID` in the sync state map for failures that are trivially forgeable (invalid PoW), reserving `BLOCK_INVALID` only for headers that passed PoW but failed contextual checks.
4. **Address the FIXME**: The existing `FIXME` comment at line 301 of `headers_process.rs` acknowledges incomplete logic; resolving it is the correct starting point.

---

### Proof of Concept

```rust
// Unit test sketch (no actual PoW needed — just craft a header with wrong nonce)
// 1. Build N headers, each with parent = genesis_hash, unique nonce (→ unique hash), invalid PoW
// 2. For each header, call HeaderAcceptor::accept()
// 3. Assert block_status_map.len() == N
// 4. Assert no remove_block_status was ever called
// 5. Repeat after simulating peer reconnect (ban expiry) to show unbounded growth

for i in 0..N {
    let header = HeaderBuilder::default()
        .parent_hash(genesis_hash.clone())
        .nonce(i.pack())  // unique hash per iteration, invalid PoW
        .build();
    let acceptor = HeaderAcceptor::new(&header, peer, verifier, active_chain.clone());
    acceptor.accept();
}
assert_eq!(shared.block_status_map().len(), N);
// No eviction occurs — map grows to N entries permanently
```

### Citations

**File:** sync/src/synchronizer/headers_process.rs (L301-303)
```rust
        // FIXME If status == BLOCK_INVALID then return early. But which error
        // type should we return?
        let status = self.active_chain.get_block_status(&self.header.hash());
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

**File:** shared/src/shared.rs (L72-72)
```rust
    pub(crate) block_status_map: Arc<DashMap<Byte32, BlockStatus>>,
```

**File:** shared/src/shared.rs (L455-457)
```rust
    pub fn insert_block_status(&self, block_hash: Byte32, status: BlockStatus) {
        self.block_status_map.insert(block_hash, status);
    }
```

**File:** chain/src/verify.rs (L141-181)
```rust
            Ok(_) => {
                let log_now = std::time::Instant::now();
                self.shared.remove_block_status(&block_hash);
                let log_elapsed_remove_block_status = log_now.elapsed();
                self.shared.remove_header_view(&block_hash);
                debug!(
                    "block {} remove_block_status cost: {:?}, and header_view cost: {:?}",
                    block_hash,
                    log_elapsed_remove_block_status,
                    log_now.elapsed()
                );
            }
            Err(err) => {
                error!("verify block {} failed: {}", block_hash, err);

                let tip = self
                    .shared
                    .store()
                    .get_tip_header()
                    .expect("tip_header must exist");
                let tip_ext = self
                    .shared
                    .store()
                    .get_block_ext(&tip.hash())
                    .expect("tip header's ext must exist");

                self.shared.set_unverified_tip(ckb_shared::HeaderIndex::new(
                    tip.number(),
                    tip.hash(),
                    tip_ext.total_difficulty,
                ));

                self.delete_unverified_block(&block);

                if !is_internal_db_error(err) {
                    self.shared
                        .insert_block_status(block_hash.clone(), BlockStatus::BLOCK_INVALID);
                } else {
                    error!("internal db error, remove block status: {}", block_hash);
                    self.shared.remove_block_status(&block_hash);
                }
```

**File:** chain/src/orphan_broker.rs (L134-155)
```rust
    pub(crate) fn clean_expired_orphans(&self) {
        debug!("clean expired orphans");
        let tip_epoch_number = self
            .shared
            .store()
            .get_tip_header()
            .expect("tip header")
            .epoch()
            .number();
        let expired_orphans = self
            .orphan_blocks_broker
            .clean_expired_blocks(tip_epoch_number);
        for expired_orphan in expired_orphans {
            self.delete_block(&expired_orphan);
            self.shared.remove_header_view(&expired_orphan.hash());
            self.shared.remove_block_status(&expired_orphan.hash());
            info!(
                "cleaned expired orphan: {}-{}",
                expired_orphan.number(),
                expired_orphan.hash()
            );
        }
```

**File:** sync/src/status.rs (L165-179)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        if !(400..500).contains(&(self.code as u16)) {
            return None;
        }
        if let Some(context) = &self.context {
            // TODO: it might be worthwhile to formalize all error texts
            // that won't be banned.
            if context.contains(ARGV_TOO_LONG_TEXT) {
                return None;
            }
        }
        match self.code {
            StatusCode::GetHeadersMissCommonAncestors => Some(SYNC_USELESS_BAN_TIME),
            _ => Some(BAD_MESSAGE_BAN_TIME),
        }
```

**File:** util/constant/src/sync.rs (L8-8)
```rust
pub const MAX_HEADERS_LEN: usize = 2_000;
```
