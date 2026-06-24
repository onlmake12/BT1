Audit Report

## Title
Peer Store Permanently Blocked by `Group::None` Flood via Discovery Protocol — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
An unprivileged remote peer can fill all 16384 `ADDR_COUNT_LIMIT` slots in the peer store with Onion3 (or other non-IP) addresses that resolve to `Group::None`. Once full, `check_purge`'s second-pass `take(len / 2)` evaluates to `take(0)` when only one group exists, evicting nothing and permanently returning `Err(EvictionFailed)` for all subsequent `add_addr` calls. The node can no longer learn about new honest peers from discovery for the lifetime of the process.

## Finding Description

**Step 1 — Fresh Onion3 entries pass `is_connectable`.**
`AddrInfo::new` initializes `attempts_count = 0` and `last_tried_at_ms = 0`. [1](#0-0) 
With those values, none of the three rejection conditions in `is_connectable` trigger:
- `tried_in_last_minute`: `0 >= now_ms - 60000` → false
- `last_connected_at_ms == 0 && attempts_count >= ADDR_MAX_RETRIES(3)`: `0 >= 3` → false
- `attempts_count >= ADDR_MAX_FAILURES(10)`: `0 >= 10` → false

So `is_connectable` returns `true` for every fresh Onion3 entry. [2](#0-1) 

**Step 2 — Onion3 addresses all map to `Group::None`.**
`Group::from(&Multiaddr)` calls `multiaddr_to_socketaddr`, which returns `None` for Onion3. The fallthrough is `Group::None`. [3](#0-2) 
All 16384 entries collapse into a single `Group::None` bucket.

**Step 3 — `take(len / 2)` integer-divides to zero.**
`check_purge`'s first pass removes non-connectable peers. Since all Onion3 entries are connectable (Step 1), `candidate_peers` is empty and execution falls into the second pass. [4](#0-3) 
In the second pass, `peers_by_network_group.len()` = 1 (one group: `Group::None`). `len / 2 = 1 / 2 = 0` in integer arithmetic. `.take(0)` yields an empty iterator, `candidate_peers` is empty, and the function returns `Err(PeerStoreError::EvictionFailed)`.

**Step 4 — `add_addr` propagates the error permanently.**
`add_addr` calls `self.check_purge()?`, propagating `Err` to every caller. [5](#0-4) 

**Step 5 — Onion3 addresses are accepted by the discovery protocol.**
`DiscoveryAddressManager::is_valid_addr` returns `true` for any address where `multiaddr_to_socketaddr` returns `None` (the `None => true` branch), which includes Onion3. [6](#0-5) 
`add_new_addrs` iterates all received addresses, filters by `is_valid_addr`, and calls `peer_store.add_addr` for each. [7](#0-6) 

**Step 6 — `AddrManager::add` deduplicates by address**, so the attacker needs 16384 unique Onion3 addresses. The Onion3 host is 10 bytes (80-bit space), making this trivially achievable. [8](#0-7) 

## Impact Explanation
After saturation, every `add_addr` call returns `Err(EvictionFailed)`. The node can no longer learn about new honest peers from discovery, cannot refresh stale entries, and is effectively isolated from organic peer discovery for the lifetime of the process. This matches **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism** — the peer store (a state storage component) enters a permanently degraded state where it cannot accept new entries.

## Likelihood Explanation
The attacker requires only a single P2P connection to the victim. Each connection can deliver one non-announce `Nodes` message (up to `MAX_ADDR_TO_SEND` items) before `received_nodes` is set and further non-announce messages trigger disconnect. With `MAX_ADDR_TO_SEND = 2500`, approximately 7 sequential connections suffice to fill all 16384 slots. No proof-of-work, no privileged role, and no Sybil majority is required. The attack is repeatable after a node restart unless the persisted peer store is cleared.

## Recommendation
Fix the `take(len / 2)` expression to guarantee at least one group is examined when the store is full:

```rust
// Before
.take(len / 2)

// After: take at least 1 group when the store needs purging
.take((len / 2).max(1))
```

Additionally, consider capping the number of `Group::None` entries accepted (e.g., reject or heavily limit non-IP addresses during `add_addr` when the store is near capacity), and rate-limit address advertisements per peer session. [9](#0-8) 

## Proof of Concept

```rust
#[test]
fn test_group_none_flood_blocks_add_addr() {
    let mut peer_store = PeerStore::default();
    // Fill store with 16384 unique Onion3 addresses (attempts_count=0, last_connected_at_ms=0)
    for i in 0u64..16384 {
        let host_bytes = i.to_le_bytes(); // 8 bytes; pad to 10
        let mut host = [0u8; 10];
        host[..8].copy_from_slice(&host_bytes);
        // Encode as base32 for onion3 host (35 chars)
        let host_b32 = base32::encode(base32::Alphabet::RFC4648 { padding: false }, &host);
        let onion_addr: Multiaddr = format!("/onion3/{}aaaa:1234", &host_b32[..31])
            .parse().unwrap();
        peer_store.mut_addr_manager()
            .add(AddrInfo::new(onion_addr, 0, 100, Flags::COMPATIBILITY.bits()));
    }
    assert_eq!(peer_store.addr_manager().count(), 16384);
    // Now the store is full with Group::None entries; adding any new addr must fail
    let ipv4_addr: Multiaddr = "/ip4/1.2.3.4/tcp/8115".parse().unwrap();
    // This returns Err(EvictionFailed) due to take(0) bug
    assert!(peer_store.add_addr(ipv4_addr, Flags::COMPATIBILITY).is_err());
}
```

### Citations

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

**File:** network/src/network_group.rs (L12-42)
```rust
impl From<&Multiaddr> for Group {
    fn from(multiaddr: &Multiaddr) -> Group {
        if let Some(socket_addr) = multiaddr_to_socketaddr(multiaddr) {
            let ip_addr = socket_addr.ip();
            if ip_addr.is_loopback() {
                return Group::LocalNetwork;
            }
            // TODO uncomment after ip feature stable
            // if !ip_addr.is_global() {
            //     // Global NetworkGroup
            //     return Group::GlobalNetwork
            // }

            // IPv4 NetworkGroup
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
            }
            // IPv6 NetworkGroup
            if let IpAddr::V6(ipv6) = ip_addr {
                if let Some(ipv4) = ipv6.to_ipv4() {
                    let bits = ipv4.octets();
                    return Group::IP4([bits[0], bits[1]]);
                }
                let bits = ipv6.octets();
                return Group::IP6([bits[0], bits[1], bits[2], bits[3]]);
            }
        }
        // Can't group addr
        Group::None
    }
```

**File:** network/src/peer_store/peer_store_impl.rs (L71-80)
```rust
    pub fn add_addr(&mut self, addr: Multiaddr, flags: Flags) -> Result<()> {
        if self.ban_list.is_addr_banned(&addr) {
            return Ok(());
        }
        self.check_purge()?;
        let score = self.score_config.default_score;
        self.addr_manager
            .add(AddrInfo::new(addr, 0, score, flags.bits()));
        Ok(())
    }
```

**File:** network/src/peer_store/peer_store_impl.rs (L357-401)
```rust
        if candidate_peers.is_empty() {
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

**File:** network/src/peer_store/addr_manager.rs (L22-34)
```rust
    pub fn add(&mut self, mut addr_info: AddrInfo) {
        if let Some(&id) = self.addr_to_id.get(&addr_info.addr) {
            let (exist_last_connected_at_ms, random_id_pos) = {
                let info = self.id_to_info.get(&id).expect("must exists");
                (info.last_connected_at_ms, info.random_id_pos)
            };
            // Get time earlier than record time, return directly
            if addr_info.last_connected_at_ms >= exist_last_connected_at_ms {
                addr_info.random_id_pos = random_id_pos;
                self.id_to_info.insert(id, addr_info);
            }
            return;
        }
```
