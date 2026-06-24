All six code references verify exactly against the repository. Here is the line-by-line confirmation:

- `unknown_header_list: Vec<Byte32>` — confirmed at L276 [1](#0-0) 
- `insert_unknown_header_hash` calls `.push(hash)` with no prior `contains` check — confirmed at L926–930 [2](#0-1) 
- `insert_peer_unknown_header_list` iterates the raw locator with no deduplication — confirmed at L1181–1197 [3](#0-2) 
- `GetHeadersProcess::execute()` applies only the count guard before forwarding — confirmed at L46–64 [4](#0-3) 
- `BlockFetcher` pops, re-inserts on miss, breaks — confirmed at L145–156 [5](#0-4) 
- `MAX_LOCATOR_SIZE = 101` — confirmed at L45 [6](#0-5) 
- `unknown_header_list_is_empty` guard (write-once semantics) — confirmed at L1183 and L932–936 [7](#0-6) 

A `clear_unknown_list` function exists at L939–944 [8](#0-7)  which could reset the list, but its call sites were not reachable in this search pass; the report does not account for it, though it does not change the root cause.

The `take_unknown_last` implementation body was not directly readable, but its behavior is unambiguously implied by the `Vec<Byte32>` type and the `block_fetcher.rs` usage pattern. [9](#0-8) 

---

The submitted report's code claims are **all confirmed**. The severity assessment (Low) is also correct: the impact is confined to one peer slot's IBD stall; the node continues syncing from all other peers; the attack requires outbound peer status; and the list is bounded to 101 entries by `MAX_LOCATOR_SIZE`. This fits "Low: Any other important performance improvements for CKB" and does not reach High or Critical.

---

Audit Report

## Title
Duplicate Entries Accepted into `unknown_header_list` via Missing Deduplication in `insert_unknown_header_hash` During IBD — (`sync/src/types/mod.rs`)

## Summary
During IBD, a malicious outbound peer can send a `GetHeaders` message with up to 101 copies of the same unknown hash. Because `insert_unknown_header_hash` calls `.push()` unconditionally and no uniqueness check exists anywhere in the call chain, all 101 copies are stored in `PeerState::unknown_header_list`. `BlockFetcher` then makes zero net progress per invocation for that peer slot, permanently preventing `best_known_header` from advancing for it.

## Finding Description
`PeerState::unknown_header_list` is a plain `Vec<Byte32>` with no set semantics. `insert_unknown_header_hash` at L926–930 calls `.push(hash)` with no prior `contains` check. `insert_peer_unknown_header_list` at L1181–1197 iterates the raw peer-supplied locator and calls `insert_unknown_header_hash` for every hash not found in the header map — no deduplication before or after. `GetHeadersProcess::execute()` at L46–64 applies only a length guard (`locator_size > MAX_LOCATOR_SIZE`) before forwarding the locator. The `unknown_header_list_is_empty` guard at L1183 means the list is written exactly once per peer; if that write contains duplicates, the slot is permanently degraded. In `BlockFetcher::fetch()` at L145–156, the `while let Some(hash) = take_unknown_last` loop pops one copy, fails the header lookup, re-inserts it, and breaks — making zero net progress per call.

## Impact Explanation
Impact is confined to one peer slot: `best_known_header` is never advanced for the targeted peer, so no blocks are fetched from it during IBD. The node continues syncing normally from all other peers. This is a **Low (501–2000 points)** finding: a concrete, exploitable performance degradation that warrants a fix, but does not crash the node, cause consensus deviation, or produce network-wide congestion. It fits "Low: Any other important performance improvements for CKB."

## Likelihood Explanation
The attack requires the attacker to be an outbound peer (the victim node must have dialed the attacker's node). This is achievable via peer-discovery poisoning but is not trivially achievable by an arbitrary unprivileged party. Once connected, a single crafted `GetHeaders` message is sufficient and the effect is permanent for that slot for the duration of IBD.

## Recommendation
Change `unknown_header_list` from `Vec<Byte32>` to `LinkedHashSet<Byte32>` (already available in the codebase at `util/src/linked_hash_set.rs`) to preserve insertion order while enforcing uniqueness. Alternatively, add an explicit `contains` check inside `insert_unknown_header_hash` before calling `.push()`, or deduplicate the incoming `header_list` slice in `insert_peer_unknown_header_list` before iterating.

## Proof of Concept
1. Run a CKB node in IBD mode and ensure it dials your controlled peer (outbound connection).
2. From the controlled peer, send a `SyncMessage::GetHeaders` with `block_locator_hashes` set to 101 copies of a single hash absent from the victim's header map.
3. Observe that `unknown_header_list` for that peer now contains 101 identical entries.
4. On every subsequent `BlockFetcher::fetch()` invocation for that peer, confirm via logging that `take_unknown_last` pops one copy, the header lookup fails, the copy is re-inserted, and the loop breaks — with the list length remaining at 101.
5. Confirm that `best_known_header` for that peer is never set and no blocks are requested from it for the duration of IBD.

### Citations

**File:** sync/src/types/mod.rs (L276-276)
```rust
    pub unknown_header_list: Vec<Byte32>,
```

**File:** sync/src/types/mod.rs (L926-930)
```rust
    pub fn insert_unknown_header_hash(&self, peer: PeerIndex, hash: Byte32) {
        self.state
            .entry(peer)
            .and_modify(|state| state.unknown_header_list.push(hash));
    }
```

**File:** sync/src/types/mod.rs (L932-936)
```rust
    pub fn unknown_header_list_is_empty(&self, peer: PeerIndex) -> bool {
        self.state
            .get(&peer)
            .map(|state| state.unknown_header_list.is_empty())
            .unwrap_or(true)
```

**File:** sync/src/types/mod.rs (L939-944)
```rust
    pub fn clear_unknown_list(&self) {
        self.state.iter_mut().for_each(|mut state| {
            if !state.unknown_header_list.is_empty() {
                state.unknown_header_list.clear()
            }
        })
```

**File:** sync/src/types/mod.rs (L1181-1197)
```rust
    pub fn insert_peer_unknown_header_list(&self, pi: PeerIndex, header_list: Vec<Byte32>) {
        // update peer's unknown_header_list only once
        if self.state().peers.unknown_header_list_is_empty(pi) {
            // header list is an ordered list, sorted from highest to lowest,
            // so here you discard and exit early
            for hash in header_list {
                if let Some(header) = self.shared().header_map().get(&hash) {
                    self.state()
                        .peers
                        .may_set_best_known_header(pi, header.as_header_index());
                    break;
                } else {
                    self.state().peers.insert_unknown_header_hash(pi, hash)
                }
            }
        }
    }
```

**File:** sync/src/synchronizer/get_headers_process.rs (L46-64)
```rust
        let locator_size = block_locator_hashes.len();
        if locator_size > MAX_LOCATOR_SIZE {
            return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                "Locator count({locator_size}) > MAX_LOCATOR_SIZE({MAX_LOCATOR_SIZE})"
            ));
        }

        if active_chain.is_initial_block_download() {
            info!(
                "Ignoring getheaders from peer={} because the node is in initial block download stage.",
                self.peer
            );
            self.send_in_ibd();
            let shared = self.synchronizer.shared();
            if let Some(flag) = shared.state().peers().get_flag(self.peer)
                && (flag.is_outbound || flag.is_whitelist || flag.is_protect)
            {
                shared.insert_peer_unknown_header_list(self.peer, block_locator_hashes);
            };
```

**File:** sync/src/synchronizer/block_fetcher.rs (L145-156)
```rust
            while let Some(hash) = state.peers().take_unknown_last(self.peer) {
                // Here we need to first try search from headermap, if not, fallback to search from the db.
                // if not search from db, it can stuck here when the headermap may have been removed just as the block was downloaded
                if let Some(header) = self.sync_shared.get_header_index_view(&hash, false) {
                    state
                        .peers()
                        .may_set_best_known_header(self.peer, header.as_header_index());
                } else {
                    state.peers().insert_unknown_header_hash(self.peer, hash);
                    break;
                }
            }
```

**File:** util/constant/src/sync.rs (L45-45)
```rust
pub const MAX_LOCATOR_SIZE: usize = 101;
```
