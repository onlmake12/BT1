### Title
Peer Store DoS via Crafted Discovery Addresses — (`network/src/peer_store/peer_store_impl.rs`)

### Summary

The `check_purge` eviction logic in `PeerStore` has two compounding conditions that can be simultaneously defeated by an attacker advertising addresses from distinct /16 subnets with ≤4 addresses per group. When the peer store reaches `ADDR_COUNT_LIMIT` (16384) under this layout, both eviction paths produce empty candidate sets and `PeerStoreError::EvictionFailed` is returned, silently preventing any new legitimate peer addresses from being added.

---

### Finding Description

`check_purge` runs two sequential eviction strategies:

**Path 1 — non-connectable eviction:** [1](#0-0) 

Addresses added via `add_addr` are created with `last_connected_at_ms = 0` and `attempts_count = 0`. The `is_connectable` check: [2](#0-1) 

returns `true` for all freshly-added addresses (no failed attempts, no timeout), so `candidate_peers` is empty and Path 1 removes nothing.

**Path 2 — network-group eviction:** [3](#0-2) 

Two conditions must both be satisfied to evict anything:
1. `take(len / 2)` — only the top half of groups (by size) are considered.
2. `if addrs.len() > 4` — only groups with **more than 4** peers are eligible.

If an attacker fills all 16384 slots with addresses from 16384 distinct /16 subnets (1 address per group), then:
- `len = 16384`, `take(8192)` processes 8192 groups
- Every group has exactly 1 address → `1 > 4` is false → `None` for every group
- `candidate_peers` is empty → `return Err(PeerStoreError::EvictionFailed)`

The `Group` type for IPv4 uses the first two octets as the key: [4](#0-3) 

so each distinct `a.b.*.*` subnet is a separate group.

**Delivery path via discovery protocol:**

The `DiscoveryAddressManager::add_new_addrs` method calls `peer_store.add_addr` for each received address and silently swallows the error: [5](#0-4) 

The discovery protocol accepts up to `MAX_ADDR_TO_SEND = 1000` nodes per message with up to `MAX_ADDRS = 3` addresses each (3000 addresses/message). Filling 16384 slots requires ~6 messages, achievable from a single connected peer across multiple rounds.

`ADDR_COUNT_LIMIT` is: [6](#0-5) 

---

### Impact Explanation

Once the peer store is saturated with attacker-controlled addresses (all connectable, all in distinct /16 groups with ≤4 per group), every subsequent call to `add_addr` from the discovery or identify protocols returns `Err(EvictionFailed)` and is silently dropped. The victim node cannot add newly discovered legitimate peer addresses. Peer discovery is effectively disabled for as long as the attacker maintains the filled state.

The attack is not permanently self-sustaining: as the node attempts to connect to the fake addresses and fails, `attempts_count` increments. After `ADDR_MAX_RETRIES = 3` failures with `last_connected_at_ms == 0`, those addresses become non-connectable and get evicted on the next `check_purge` call. However, the attacker can continuously re-advertise fresh addresses to keep the store full, sustaining the DoS.

---

### Likelihood Explanation

- Requires only a single inbound or outbound P2P connection to the victim.
- No privileged access, no PoW, no key material needed.
- The discovery protocol imposes no per-IP or per-session rate limit on how many distinct /16 subnets can be advertised.
- The attack is local-testable: fill a `PeerStore` with 16384 addresses from distinct /16 subnets, then call `add_addr` for a new address and observe `Err(EvictionFailed)`.

---

### Recommendation

Fix both conditions in the network-group eviction path:

1. **Remove the `take(len / 2)` truncation** or change it to consider all groups, not just the top half.
2. **Lower or remove the `> 4` threshold** for eviction eligibility, or always evict at least one address from the largest group regardless of its size.
3. **Add a per-session or per-/16 rate limit** in `add_new_addrs` to bound how many distinct subnets a single peer can contribute to the store.
4. **Treat `EvictionFailed` as a warning-level log** (not debug) so operators can detect the condition.

---

### Proof of Concept

```rust
let mut peer_store = PeerStore::default();
// Fill with 16384 addresses, one per distinct /16 subnet, all connectable
for i in 0u32..16384 {
    let a = (i >> 8) as u8;
    let b = (i & 0xff) as u8;
    let addr: Multiaddr = format!(
        "/ip4/{}.{}.0.1/tcp/8114/p2p/{}",
        a, b,
        PeerId::random().to_base58()
    ).parse().unwrap();
    peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
}
assert_eq!(peer_store.addr_manager().count(), 16384);

// Now attempt to add a new legitimate address
let new_addr: Multiaddr = format!(
    "/ip4/200.200.200.1/tcp/8114/p2p/{}",
    PeerId::random().to_base58()
).parse().unwrap();
// This returns Err(PeerStore(EvictionFailed))
assert!(peer_store.add_addr(new_addr, Flags::COMPATIBILITY).is_err());
```

### Citations

**File:** network/src/peer_store/peer_store_impl.rs (L341-355)
```rust
        let candidate_peers: Vec<_> = self
            .addr_manager
            .addrs_iter()
            .filter_map(|addr| {
                if !addr.is_connectable(now_ms) {
                    Some(addr.addr.clone())
                } else {
                    None
                }
            })
            .collect();

        for key in candidate_peers.iter() {
            self.addr_manager.remove(key);
        }
```

**File:** network/src/peer_store/peer_store_impl.rs (L358-401)
```rust
            let candidate_peers: Vec<_> = {
                let mut peers_by_network_group: HashMap<Group, Vec<_>> = HashMap::default();
                for addr in self.addr_manager.addrs_iter() {
                    peers_by_network_group
                        .entry((&addr.addr).into())
                        .or_default()
                        .push(addr);
                }
                let len = peers_by_network_group.len();
                let mut peers = peers_by_network_group
                    .drain()
                    .map(|(_, v)| v)
                    .collect::<Vec<Vec<_>>>();

                peers.sort_unstable_by_key(|k| std::cmp::Reverse(k.len()));

                peers
                    .into_iter()
                    .take(len / 2)
                    .flat_map(move |addrs| {
                        if addrs.len() > 4 {
                            Some(
                                addrs
                                    .iter()
                                    .choose_multiple(&mut rand::thread_rng(), 2)
                                    .into_iter()
                                    .map(|addr| addr.addr.clone())
                                    .collect::<Vec<Multiaddr>>(),
                            )
                        } else {
                            None
                        }
                    })
                    .flatten()
                    .collect()
            };

            for key in candidate_peers.iter() {
                self.addr_manager.remove(key);
            }

            if candidate_peers.is_empty() {
                return Err(PeerStoreError::EvictionFailed.into());
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

**File:** network/src/network_group.rs (L26-29)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
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
