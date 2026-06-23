Based on my code analysis, here is my assessment:

---

### Title
Premature `add_anchors` Before Identify Handshake Allows Attacker-Controlled Address Persistence in `anchors.db` — (`network/src/peer_registry.rs`)

### Summary

`accept_peer` unconditionally calls `peer_store.add_anchors(remote_addr)` at the moment of TCP session open, before the identify protocol completes. There is no corresponding removal of the anchor when the session is subsequently closed for any reason (identify failure, ban, or disconnect). On shutdown, the in-memory anchor set is flushed to `anchors.db`, and on every restart the node dials all anchors. An attacker whose address is in the victim's peer store can exploit this to achieve persistent forced reconnection on every node restart.

### Finding Description

In `accept_peer`, when `non_whitelist_outbound >= max_outbound` and `block_relay_only_outbound_count < MAX_OUTBOUND_BLOCK_RELAY` (2), the following executes immediately upon `SessionOpen`:

```rust
peer_store.add_anchors(remote_addr.clone());   // line 130
session_type = SessionType::BlockRelayOnly;    // line 131
``` [1](#0-0) 

`add_anchors` simply inserts into an in-memory `HashSet<Multiaddr>` with no preconditions:

```rust
pub fn add_anchors(&mut self, addr: Multiaddr) {
    self.anchors.add(addr);
}
``` [2](#0-1) 

The `Anchors` struct exposes `add`, `count`, `dump_iter`, `drain`, and `contains` — **no `remove` method exists**: [3](#0-2) 

When the session subsequently closes (identify failure, ban, or any disconnect), the `SessionClose` handler calls only `remove_disconnected_peer`, which removes from `connected_peers` but **not from `anchors`**:

```rust
self.network_state.with_peer_store_mut(|peer_store| {
    peer_store.remove_disconnected_peer(&session_context.address);
});
``` [4](#0-3) 

```rust
pub fn remove_disconnected_peer(&mut self, addr: &Multiaddr) -> Option<PeerInfo> {
    extract_peer_id(addr).and_then(|peer_id| self.connected_peers.remove(&peer_id))
}
``` [5](#0-4) 

On shutdown, `DumpPeerStoreService::drop` calls `dump_to_dir`, which writes the current (unmodified) anchor set to `anchors.db`: [6](#0-5) [7](#0-6) 

On every restart, `NetworkService::start` drains anchors and dials them unconditionally:

```rust
let anchors: Vec<_> = peer_store.mut_anchors().drain().collect();
addrs.extend(anchors);
``` [8](#0-7) 

### Impact Explanation

An attacker whose address is in the victim's peer store (reachable via normal P2P gossip/discovery) can occupy up to `MAX_OUTBOUND_BLOCK_RELAY = 2` anchor slots persistently. On every node restart, the victim unconditionally dials the attacker's address. Even if the attacker is subsequently banned, the ban is time-limited and the anchor entry survives. This enables long-term forced reconnection and partial eclipse of block-relay-only outbound connections.

### Likelihood Explanation

The preconditions are realistic: the victim's regular outbound slots must be full (normal under load), and the attacker's address must be in the peer store (achievable via P2P gossip). The attacker does not need to maintain a valid CKB node — they only need to accept TCP connections long enough for `SessionOpen` to fire, then drop the connection or fail identify. The attack is repeatable on every restart.

### Recommendation

Remove the anchor entry when the corresponding session closes without completing a successful identify handshake. Add a `remove` method to `Anchors` and call it from the `SessionClose` handler (or from the identify `disconnected`/failure path) when `is_anchor(session_id)` is true and identify was never successfully completed. Anchors should only be persisted for peers that completed the full protocol handshake.

### Proof of Concept

1. Fill victim's `max_outbound` regular outbound slots with legitimate peers.
2. Ensure attacker address is in victim's peer store (via discovery gossip).
3. Victim dials attacker → `accept_peer` → `add_anchors(attacker_addr)` called at line 130.
4. Attacker drops TCP connection immediately (identify never completes).
5. `SessionClose` fires → `remove_disconnected_peer` called → anchor NOT removed.
6. Shutdown victim → `dump_to_dir` writes attacker address to `anchors.db`.
7. Restart victim → `mut_anchors().drain()` → attacker address dialed again.
8. Assert: attacker address present in `anchors.db` after step 6; victim dials attacker on step 7.

### Citations

**File:** network/src/peer_registry.rs (L123-133)
```rust
            } else if connection_status.non_whitelist_outbound >= self.max_outbound {
                if self.disable_block_relay_only_connection
                    || connection_status.block_relay_only_outbound_count
                        >= self.max_outbound_block_relay
                {
                    return Err(PeerError::ReachMaxOutboundLimit.into());
                } else {
                    peer_store.add_anchors(remote_addr.clone());
                    session_type = SessionType::BlockRelayOnly;
                }
            }
```

**File:** network/src/peer_store/peer_store_impl.rs (L117-119)
```rust
    pub fn add_anchors(&mut self, addr: Multiaddr) {
        self.anchors.add(addr);
    }
```

**File:** network/src/peer_store/peer_store_impl.rs (L170-172)
```rust
    pub fn remove_disconnected_peer(&mut self, addr: &Multiaddr) -> Option<PeerInfo> {
        extract_peer_id(addr).and_then(|peer_id| self.connected_peers.remove(&peer_id))
    }
```

**File:** network/src/peer_store/anchors.rs (L15-39)
```rust
impl Anchors {
    /// Add an address information to anchors
    pub fn add(&mut self, addr: Multiaddr) {
        self.addrs.insert(addr);
    }

    /// The count of address in anchors
    pub fn count(&self) -> usize {
        self.addrs.len()
    }

    /// Anchors dump iterator, take MAX_OUTBOUND_BLOCK_RELAY
    pub fn dump_iter(&self) -> impl Iterator<Item = &Multiaddr> {
        self.addrs.iter().take(MAX_OUTBOUND_BLOCK_RELAY as usize)
    }

    /// Anchors drain
    pub fn drain(&mut self) -> impl Iterator<Item = Multiaddr> {
        self.addrs.drain()
    }

    /// Whether Anchors contains specified addr
    pub fn contains(&self, addr: &Multiaddr) -> bool {
        self.addrs.contains(addr)
    }
```

**File:** network/src/network.rs (L810-812)
```rust
                    self.network_state.with_peer_store_mut(|peer_store| {
                        peer_store.remove_disconnected_peer(&session_context.address);
                    });
```

**File:** network/src/network.rs (L1187-1188)
```rust
            let anchors: Vec<_> = peer_store.mut_anchors().drain().collect();
            addrs.extend(anchors);
```

**File:** network/src/services/dump_peer_store.rs (L49-62)
```rust
impl Drop for DumpPeerStoreService {
    fn drop(&mut self) {
        debug!("Dump peer store before exiting");
        self.dump_peer_store();
        #[cfg(target_family = "wasm")]
        {
            use crate::peer_store::browser::get_db;
            let path = self.network_state.config.peer_store_path();
            p2p::runtime::spawn(async {
                let _ignore = get_db(path).await.shutdown().await;
            });
        }
    }
}
```

**File:** network/src/peer_store/peer_store_db.rs (L249-250)
```rust
        self.anchors().dump(dump_open(&tmp_anchors_list)?)?;
        move_file(tmp_anchors_list, path.as_ref().join(DEFAULT_ANCHORS_DB))?;
```
