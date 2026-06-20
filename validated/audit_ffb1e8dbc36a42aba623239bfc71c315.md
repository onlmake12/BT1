### Title
`check_purge` `take(len/2)` Integer-Division Zero Causes Permanent `EvictionFailed` When All Addresses Share One /16 Subnet — (`network/src/peer_store/peer_store_impl.rs`)

---

### Summary

`PeerStore::check_purge` uses integer division `take(len / 2)` to select the top half of network groups for eviction. When all stored addresses belong to a single /16 subnet, `peers_by_network_group.len() == 1`, so `1 / 2 == 0` in Rust integer arithmetic, and `take(0)` yields no candidates. The function returns `Err(PeerStoreError::EvictionFailed)`, permanently blocking `add_addr` from accepting any new peer — even though 16 384 evictable addresses exist in that one group.

---

### Finding Description

**Root cause — integer division to zero:** [1](#0-0) 

```
let len = peers_by_network_group.len();   // = 1 when all addrs share one /16
...
peers
    .into_iter()
    .take(len / 2)          // 1 / 2 == 0  →  no groups iterated
    .flat_map(|addrs| { ... })
    .flatten()
    .collect()              // always empty
```

When `candidate_peers` is empty the function falls through to: [2](#0-1) 

**Group key is the first two octets of the IPv4 address:** [3](#0-2) 

Every address in `A.B.0.0/16` maps to `Group::IP4([A, B])` — a single HashMap key.

**Freshly discovered addresses are always connectable:** [4](#0-3) 

`add_addr` stores `last_connected_at_ms = 0` and `attempts_count = 0`. [5](#0-4) 

With `attempts_count = 0`, neither early-return condition fires, so `is_connectable` returns `true` for all injected addresses. The first pass (evict non-connectable) removes nothing, and execution falls into the broken second pass.

**`ADDR_COUNT_LIMIT` is 16 384:** [6](#0-5) 

**P2P entry point — discovery `Nodes` messages:** [7](#0-6) 

`add_new_addrs` iterates attacker-supplied addresses and calls `peer_store.add_addr`. Errors are silently swallowed as `debug!` logs, so the caller never disconnects the attacker.

Per-message limits allow up to `MAX_ADDR_TO_SEND = 1000` nodes × `MAX_ADDRS = 3` addresses = 3 000 addresses per `Nodes` message. [8](#0-7) 

Six such messages (across one or more connections) saturate the 16 384-entry store. All addresses must be in a publicly routable /16 subnet to pass `is_reachable`.

---

### Impact Explanation

Once the store is full with single-subnet addresses, every subsequent `add_addr` call hits `check_purge`, which returns `Err(EvictionFailed)`. The node can no longer learn about any new peers via discovery. Existing connections are unaffected, but after those peers disconnect the node cannot replenish its peer set, degrading or eventually severing its network connectivity.

---

### Likelihood Explanation

An attacker needs one or more P2P connections to the victim and the ability to send crafted `Nodes` messages with ~16 384 distinct addresses from a single public /16 block. No PoW, no key material, and no privileged access is required. The attack is repeatable after a node restart because the peer store is persisted to disk.

---

### Recommendation

Replace `take(len / 2)` with a formulation that always includes at least one group when the store is full:

```rust
// e.g., take at least 1, or take ceil(len / 2)
let take_count = std::cmp::max(1, len / 2);
peers.into_iter().take(take_count)...
```

Additionally, enforce a per-/16-subnet cap during `add_addr` (e.g., reject a new address if its group already holds more than `ADDR_COUNT_LIMIT / expected_groups` entries) to prevent a single subnet from monopolising the store in the first place.

---

### Proof of Concept

```rust
#[test]
fn test_single_subnet_eviction_deadlock() {
    use crate::peer_store::{ADDR_COUNT_LIMIT, PeerStore};
    use crate::{Flags, PeerId};
    use p2p::multiaddr::Multiaddr;

    let mut peer_store = PeerStore::default();
    // Fill store with ADDR_COUNT_LIMIT connectable addresses, all in 1.2.0.0/16
    for i in 0..ADDR_COUNT_LIMIT {
        let ip = format!("1.2.{}.{}", (i >> 8) & 0xff, i & 0xff);
        let addr: Multiaddr = format!(
            "/ip4/{}/tcp/8114/p2p/{}",
            ip,
            PeerId::random().to_base58()
        )
        .parse()
        .unwrap();
        peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
    // Store is now full; attempt to add one more address from a different subnet
    let new_addr: Multiaddr = format!(
        "/ip4/8.8.8.8/tcp/8114/p2p/{}",
        PeerId::random().to_base58()
    )
    .parse()
    .unwrap();
    // Expect Ok — but actually returns Err(EvictionFailed)
    assert!(
        peer_store.add_addr(new_addr, Flags::COMPATIBILITY).is_ok(),
        "check_purge must evict at least one peer when store is full"
    );
    assert!(peer_store.addr_manager().count() < ADDR_COUNT_LIMIT);
}
```

### Citations

**File:** network/src/peer_store/peer_store_impl.rs (L76-79)
```rust
        let score = self.score_config.default_score;
        self.addr_manager
            .add(AddrInfo::new(addr, 0, score, flags.bits()));
        Ok(())
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

**File:** network/src/network_group.rs (L26-29)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
            }
```

**File:** network/src/peer_store/types.rs (L89-105)
```rust
    pub fn is_connectable(&self, now_ms: u64) -> bool {
        // do not remove addr tried in last minute
        if self.tried_in_last_minute(now_ms) {
            return true;
        }
        // we give up if never connect to this addr
        if self.last_connected_at_ms == 0 && self.attempts_count >= ADDR_MAX_RETRIES {
            return false;
        }
        // consider addr is not connectable if failed too many times
        if now_ms.saturating_sub(self.last_connected_at_ms) > ADDR_TIMEOUT_MS
            && (self.attempts_count >= ADDR_MAX_FAILURES)
        {
            return false;
        }
        true
    }
```

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```

**File:** network/src/protocols/discovery/mod.rs (L266-299)
```rust
fn verify_nodes_message(nodes: &Nodes) -> Option<Misbehavior> {
    let mut misbehavior = None;
    if nodes.announce {
        if nodes.items.len() > ANNOUNCE_THRESHOLD {
            warn!(
                "Number of nodes exceeds announce threshold {}",
                ANNOUNCE_THRESHOLD
            );
            misbehavior = Some(Misbehavior::TooManyItems {
                announce: nodes.announce,
                length: nodes.items.len(),
            });
        }
    } else if nodes.items.len() > MAX_ADDR_TO_SEND {
        warn!(
            "Too many items (announce=false) length={}",
            nodes.items.len()
        );
        misbehavior = Some(Misbehavior::TooManyItems {
            announce: nodes.announce,
            length: nodes.items.len(),
        });
    }

    if misbehavior.is_none() {
        for item in &nodes.items {
            if item.addresses.len() > MAX_ADDRS {
                misbehavior = Some(Misbehavior::TooManyAddresses(item.addresses.len()));
                break;
            }
        }
    }

    misbehavior
```

**File:** network/src/protocols/discovery/mod.rs (L347-362)
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
```
