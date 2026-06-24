All cited code references are confirmed against the actual source. Every claim in the report is accurate:

- `take(len / 2)` at line 376 of `peer_store_impl.rs` — integer truncation to `take(0)` when `len == 1` is real.
- Onion3 → `Group::None` in `network_group.rs` lines 39–41 is real.
- `add_addr` stores `last_connected_at_ms = 0`, `attempts_count = 0` (`types.rs` lines 65–76, `peer_store_impl.rs` line 78).
- `is_connectable` returns `true` for those entries (`types.rs` lines 95–96: condition requires `attempts_count >= 3`).
- `is_valid_addr` passes Onion3 via `None => true` (`mod.rs` lines 334–336).
- Announce messages bypass `DuplicateFirstNodes` guard (`mod.rs` lines 181–205).
- `Err(EvictionFailed)` returned at `peer_store_impl.rs` lines 399–401.

---

Audit Report

## Title
Peer Store Permanently Blocked by `Group::None` Flooding via Onion3 Discovery Addresses — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
`check_purge`'s second-pass eviction uses `take(len / 2)` where `len` is the number of distinct network groups. When all 16,384 stored addresses are Onion3 (which all hash to `Group::None`), `len == 1` and integer division yields `take(0)`, so zero peers are evicted and `check_purge` returns `Err(PeerStoreError::EvictionFailed)`. An attacker with a single P2P connection can stream unique Onion3 addresses via repeated `announce=true` `Nodes` messages to saturate the peer store, permanently preventing the victim node from recording newly discovered IPv4/IPv6 peers until restart.

## Finding Description

**Root cause — `take(len / 2)` integer truncation:**

In `check_purge`, after the first pass finds no non-connectable entries, the second pass groups all stored addresses by network group and takes the top `len / 2` groups for eviction candidates: [1](#0-0) 

When all stored addresses are Onion3, `peers_by_network_group` has exactly one key (`Group::None`), so `len = 1` and `take(1 / 2)` = `take(0)`. The iterator yields nothing, `candidate_peers` is empty, and the function returns: [2](#0-1) 

**Why Onion3 addresses all map to `Group::None`:**

`Group::from(&Multiaddr)` calls `multiaddr_to_socketaddr`, which returns `None` for Onion3 addresses. The fallthrough is: [3](#0-2) 

**Why the first pass (non-connectable filter) also finds nothing:**

`add_addr` always stores addresses with `last_connected_at_ms = 0` and `attempts_count = 0`: [4](#0-3) [5](#0-4) 

`is_connectable` returns `true` for such entries because the only disqualifying condition for never-connected addresses requires `attempts_count >= ADDR_MAX_RETRIES (3)`, which is false at `0`: [6](#0-5) 

**Attacker entry point — discovery `Nodes` messages:**

`add_new_addrs` in `DiscoveryAddressManager` calls `peer_store.add_addr` for every address received via the discovery protocol, with no per-peer rate limit: [7](#0-6) 

`is_valid_addr` passes Onion3 addresses unconditionally via the `None => true` branch (since `multiaddr_to_socketaddr` returns `None` for Onion3): [8](#0-7) 

`announce=true` `Nodes` messages are processed on every receipt without the `DuplicateFirstNodes` guard, which only applies to `announce=false` messages: [9](#0-8) 

Each announce message is limited to `ANNOUNCE_THRESHOLD = 10` items with up to `MAX_ADDRS = 3` addresses each (30 addresses/message), so filling `ADDR_COUNT_LIMIT = 16384` slots requires approximately 547 messages — trivially achievable over a single persistent connection. [10](#0-9) 

## Impact Explanation

Once the peer store is saturated with 16,384 `Group::None` entries, every call to `add_addr` returns `Err(PeerStoreError::EvictionFailed)`. The node can no longer record newly discovered IPv4/IPv6 peers. Its ability to bootstrap connections, reconnect after disconnections, and maintain a healthy peer set is permanently degraded until the node is restarted and the peer store is cleared. This matches **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism**.

## Likelihood Explanation

An attacker needs only a single P2P connection to the victim. Onion3 addresses are valid multiaddrs accepted by the protocol parser and pass `is_valid_addr` unconditionally. The attacker sends repeated `announce=true` `Nodes` messages, each containing unique Onion3 multiaddrs. No PoW, no privileged role, no key material, and no Sybil attack is required. The attack is fully executable from a single unprivileged peer and is permanent until node restart.

## Recommendation

Fix the `take(len / 2)` truncation at `peer_store_impl.rs` line 376. Replace with `take((len + 1) / 2)` (ceiling division) or `take(len.max(1))` to ensure at least one group is always considered for eviction when the second pass is entered. Additionally, consider capping the number of `Group::None` entries accepted into the peer store (e.g., reject or rate-limit Onion3 addresses once they exceed a configurable fraction of `ADDR_COUNT_LIMIT`).

## Proof of Concept

```rust
#[test]
fn test_onion3_group_none_eviction_deadlock() {
    use crate::{Flags, PeerId, peer_store::{ADDR_COUNT_LIMIT, PeerStore}};
    use p2p::multiaddr::Multiaddr;

    let mut peer_store = PeerStore::default();

    // Fill the store with ADDR_COUNT_LIMIT unique Onion3 addresses.
    // Each maps to Group::None; all have attempts_count=0, last_connected_at_ms=0 (connectable).
    for i in 0..ADDR_COUNT_LIMIT {
        let onion_addr: Multiaddr = format!(
            "/onion3/vww6ybal4bd7szmgncyruucpgfkqahzddi37ktceo3ah7ngmcopnpyyd:{}/p2p/{}",
            1024 + i,
            PeerId::random().to_base58()
        ).parse().unwrap();
        let _ = peer_store.add_addr(onion_addr, Flags::COMPATIBILITY);
    }

    // Now the store is at capacity with all Group::None entries.
    // Attempting to add a legitimate IPv4 peer must succeed after eviction.
    let legit: Multiaddr = format!(
        "/ip4/1.2.3.4/tcp/8115/p2p/{}", PeerId::random().to_base58()
    ).parse().unwrap();

    // This returns Err(EvictionFailed) — the invariant is broken.
    assert!(
        peer_store.add_addr(legit, Flags::COMPATIBILITY).is_ok(),
        "peer store must be able to accept legitimate peers after eviction"
    );
}
```

### Citations

**File:** network/src/peer_store/peer_store_impl.rs (L77-78)
```rust
        self.addr_manager
            .add(AddrInfo::new(addr, 0, score, flags.bits()));
```

**File:** network/src/peer_store/peer_store_impl.rs (L366-376)
```rust
                let len = peers_by_network_group.len();
                let mut peers = peers_by_network_group
                    .drain()
                    .map(|(_, v)| v)
                    .collect::<Vec<Vec<_>>>();

                peers.sort_unstable_by_key(|k| std::cmp::Reverse(k.len()));

                peers
                    .into_iter()
                    .take(len / 2)
```

**File:** network/src/peer_store/peer_store_impl.rs (L399-401)
```rust
            if candidate_peers.is_empty() {
                return Err(PeerStoreError::EvictionFailed.into());
            }
```

**File:** network/src/network_group.rs (L39-42)
```rust
        }
        // Can't group addr
        Group::None
    }
```

**File:** network/src/peer_store/types.rs (L65-76)
```rust
    pub fn new(addr: Multiaddr, last_connected_at_ms: u64, score: Score, flags: u64) -> Self {
        AddrInfo {
            // only store tcp protocol
            addr: base_addr(&addr),
            score,
            last_connected_at_ms,
            last_tried_at_ms: 0,
            attempts_count: 0,
            random_id_pos: 0,
            flags,
        }
    }
```

**File:** network/src/peer_store/types.rs (L94-97)
```rust
        // we give up if never connect to this addr
        if self.last_connected_at_ms == 0 && self.attempts_count >= ADDR_MAX_RETRIES {
            return false;
        }
```

**File:** network/src/protocols/discovery/mod.rs (L180-206)
```rust
                        if let Some(state) = self.sessions.get_mut(&session.id) {
                            if !nodes.announce && state.received_nodes {
                                warn!("Nodes (announce=false) message received");
                                if check(Misbehavior::DuplicateFirstNodes)
                                    && context.disconnect(session.id).await.is_err()
                                {
                                    debug!("Disconnect {:?} msg failed to send", session.id)
                                }
                            } else {
                                let addrs = nodes
                                    .items
                                    .into_iter()
                                    .flat_map(|node| {
                                        node.addresses.into_iter().map(move |a| (a, node.flags))
                                    })
                                    .collect::<Vec<_>>();

                                state.addr_known.extend(addrs.iter());
                                // Non-announce nodes can only receive once
                                // Due to the uncertainty of the other party’s state,
                                // the announce node may be sent out first, and it must be
                                // determined to be Non-announce before the state can be changed
                                if !nodes.announce {
                                    state.received_nodes = true;
                                }
                                self.addr_mgr.add_new_addrs(session.id, addrs);
                            }
```

**File:** network/src/protocols/discovery/mod.rs (L332-341)
```rust
    fn is_valid_addr(&self, addr: &Multiaddr) -> bool {
        if !self.discovery_local_address {
            match multiaddr_to_socketaddr(addr) {
                Some(socket_addr) => is_reachable(socket_addr.ip()),
                None => true,
            }
        } else {
            true
        }
    }
```

**File:** network/src/protocols/discovery/mod.rs (L347-363)
```rust
    fn add_new_addrs(&mut self, _session_id: SessionId, addrs: Vec<(Multiaddr, Flags)>) {
        if addrs.is_empty() {
            return;
        }

        for (addr, flags) in addrs.into_iter().filter(|addr| self.is_valid_addr(&addr.0)) {
            trace!("Add discovered address:{:?}", addr);
            self.network_state.with_peer_store_mut(|peer_store| {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    debug!(
                        "Failed to add discovered address to peer_store {:?} {:?}",
                        err, addr
                    );
                }
            });
        }
    }
```

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```
