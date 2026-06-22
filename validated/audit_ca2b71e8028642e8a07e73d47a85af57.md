### Title
Peer-Supplied Arbitrary Block Locator Hashes Permanently Corrupt IBD Peer Sync State — (`sync/src/synchronizer/get_headers_process.rs`)

### Summary
During Initial Block Download (IBD), when a node receives a `GetHeaders` message from an outbound, whitelist, or protect peer, the peer's block locator hashes are inserted into `unknown_header_list` without validating that those hashes correspond to real blocks. Because the list is only populated **once** per peer (guarded by an `is_empty` check), a malicious outbound peer can permanently corrupt the node's sync state for that connection by sending fabricated block locator hashes, preventing the node from ever selecting that peer for `GetHeaders` requests during IBD.

### Finding Description

In `sync/src/synchronizer/get_headers_process.rs`, when the local node is in IBD and receives a `GetHeaders` message from an outbound/whitelist/protect peer, it calls `insert_peer_unknown_header_list` with the raw, peer-supplied `block_locator_hashes`:

```rust
if let Some(flag) = shared.state().peers().get_flag(self.peer)
    && (flag.is_outbound || flag.is_whitelist || flag.is_protect)
{
    shared.insert_peer_unknown_header_list(self.peer, block_locator_hashes);
};
``` [1](#0-0) 

`insert_peer_unknown_header_list` (in `sync/src/types/mod.rs`) iterates the supplied hashes and inserts every hash **not found in the local header_map** into the peer's `unknown_header_list`, with no check that the hash corresponds to any real block in the chain store:

```rust
pub fn insert_peer_unknown_header_list(&self, pi: PeerIndex, header_list: Vec<Byte32>) {
    if self.state().peers.unknown_header_list_is_empty(pi) {
        for hash in header_list {
            if let Some(header) = self.shared().header_map().get(&hash) {
                self.state().peers.may_set_best_known_header(pi, header.as_header_index());
                break;
            } else {
                self.state().peers.insert_unknown_header_hash(pi, hash)
            }
        }
    }
}
``` [2](#0-1) 

The `is_empty` guard means the list is populated **only once** per peer connection. Once populated with fake hashes, the `BlockFetcher` (in `sync/src/synchronizer/block_fetcher.rs`) processes the list by popping each hash and looking it up in the header_map. When a fake hash is not found, it is **re-inserted** and the loop breaks:

```rust
while let Some(hash) = state.peers().take_unknown_last(self.peer) {
    if let Some(header) = self.sync_shared.get_header_index_view(&hash, false) {
        state.peers().may_set_best_known_header(self.peer, header.as_header_index());
    } else {
        state.peers().insert_unknown_header_hash(self.peer, hash);
        break;
    }
}
``` [3](#0-2) 

This means the list is **never drained** — it always retains at least one unresolvable fake hash. The function `get_best_known_less_than_tip_and_unknown_empty` (used to select peers for outgoing `GetHeaders` requests) explicitly excludes any peer whose `unknown_header_list` is non-empty:

```rust
if !state.unknown_header_list.is_empty() {
    return None;
}
``` [4](#0-3) 

The `PeerState` struct holding `unknown_header_list` is defined as:

```rust
pub unknown_header_list: Vec<Byte32>,
``` [5](#0-4) 

There is no mechanism to clear `unknown_header_list` for a connected peer other than disconnection. The `clear_unknown_list` helper exists but is not called in any reachable production path during normal IBD operation. [6](#0-5) 

### Impact Explanation

For the duration of the peer connection, the node's IBD sync state for that peer is permanently corrupted:

1. `best_known_header` for the peer is never updated via the `unknown_header_list` path.
2. The peer is permanently excluded from `get_best_known_less_than_tip_and_unknown_empty`, so the node never issues `GetHeaders` to that peer during IBD.
3. If an attacker controls multiple outbound peers (e.g., by populating the peer store via DNS seeding or peer exchange), they can impair the node's IBD progress across multiple connections simultaneously.

The analog to the original report is exact: arbitrary identifiers (block locator hashes instead of validator pubkeys) are accepted without registry validation, state is mutated (`unknown_header_list` populated), and future legitimate operations (outgoing `GetHeaders` to that peer) are blocked.

### Likelihood Explanation

The attacker only needs to be an outbound peer of the victim node — a realistic position achievable by being present in the peer store (via DNS seeding, peer exchange, or direct advertisement). The attack requires sending a single `GetHeaders` message with fabricated 32-byte hashes during IBD, which is trivially constructable. No privileged access, key material, or majority hashpower is required.

### Recommendation

Before inserting a hash into `unknown_header_list`, validate it against the chain store (not just the in-memory header_map). Hashes that are absent from both the header_map and the persistent store should be rejected rather than stored. Additionally, consider adding a mechanism to clear or expire stale entries in `unknown_header_list` for connected peers, rather than relying solely on peer disconnection.

### Proof of Concept

1. Node A enters IBD.
2. Attacker controls peer B, which connects to Node A as an outbound peer.
3. Peer B sends a `GetHeaders` (Sync protocol) message containing `MAX_LOCATOR_SIZE` random 32-byte values as `block_locator_hashes`.
4. Node A, in IBD, calls `insert_peer_unknown_header_list(B, fake_hashes)`.
5. Since `unknown_header_list` for B is empty, all fake hashes are inserted.
6. On each `BlockFetcher` tick for peer B, the last fake hash is popped, not found in the header_map, re-inserted, and the loop breaks — the list is never cleared.
7. `get_best_known_less_than_tip_and_unknown_empty` permanently excludes peer B.
8. Node A never issues `GetHeaders` to peer B for the remainder of the connection, losing that peer as a sync source during IBD.

### Citations

**File:** sync/src/synchronizer/get_headers_process.rs (L59-64)
```rust
            let shared = self.synchronizer.shared();
            if let Some(flag) = shared.state().peers().get_flag(self.peer)
                && (flag.is_outbound || flag.is_whitelist || flag.is_protect)
            {
                shared.insert_peer_unknown_header_list(self.peer, block_locator_hashes);
            };
```

**File:** sync/src/types/mod.rs (L276-277)
```rust
    pub unknown_header_list: Vec<Byte32>,
}
```

**File:** sync/src/types/mod.rs (L939-945)
```rust
    pub fn clear_unknown_list(&self) {
        self.state.iter_mut().for_each(|mut state| {
            if !state.unknown_header_list.is_empty() {
                state.unknown_header_list.clear()
            }
        })
    }
```

**File:** sync/src/types/mod.rs (L955-957)
```rust
                if !state.unknown_header_list.is_empty() {
                    return None;
                }
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
