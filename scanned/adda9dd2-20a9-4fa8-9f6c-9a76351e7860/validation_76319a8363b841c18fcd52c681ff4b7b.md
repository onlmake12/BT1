Now I have all the code needed to fully trace the vulnerability. Let me verify the exact integer division behavior in `check_purge`.

### Title
Group::None Integer-Division Eviction Bypass Causes Permanent Peer Store DoS — (`network/src/peer_store/peer_store_impl.rs`)

---

### Summary

When all 16 384 slots in `AddrManager` are occupied by addresses that resolve to `Group::None` (i.e., addresses for which `multiaddr_to_socketaddr` returns `None`), the group-based eviction path in `check_purge` computes `take(len / 2) = take(1 / 2) = take(0)` due to integer division, selects zero eviction candidates, and unconditionally returns `PeerStoreError::EvictionFailed`. Every subsequent call to `add_addr` — including calls for legitimate, connectable IPv4/IPv6 peers — propagates this error, permanently blocking new peer discovery until the node is restarted.

---

### Finding Description

**Entry point:** The discovery protocol handler at `network/src/protocols/discovery/mod.rs` calls `PeerStore::add_addr` for every address advertised by a remote peer. No IP-format validation is enforced before the call.

**`add_addr` → `check_purge` call chain:** [1](#0-0) 

`check_purge` has two eviction stages:

**Stage 1 — connectable filter:** Collects addresses where `!addr.is_connectable(now_ms)`. [2](#0-1) 

Addresses inserted by `add_addr` are created with `last_connected_at_ms = 0` and `attempts_count = 0`. [3](#0-2) 

Because `attempts_count (0) < ADDR_MAX_RETRIES (3)`, `is_connectable` returns `true` for every freshly added address. Stage 1 finds nothing to evict and `candidate_peers` is empty.

**Stage 2 — group-based eviction:** [4](#0-3) 

The critical line is:

```rust
let len = peers_by_network_group.len();   // = 1 when all are Group::None
peers.into_iter().take(len / 2)           // take(1/2) = take(0)
```

When every stored address maps to `Group::None` (because `multiaddr_to_socketaddr` returns `None` for them), `peers_by_network_group` has exactly **one** key. Integer division gives `1 / 2 = 0`, so `take(0)` yields an empty iterator. `candidate_peers` is empty, and the function returns `Err(PeerStoreError::EvictionFailed)`.

**`Group::None` is produced by:** [5](#0-4) 

Any multiaddr without a resolvable IP component — bare `/p2p/<peer-id>`, DNS names, Onion3, etc. — falls through to `Group::None`.

**Capacity constant:** [6](#0-5) 

---

### Impact Explanation

Once the peer store is saturated with `Group::None` entries, `add_addr` returns `EvictionFailed` for every call, including calls for valid IPv4/IPv6 peers received via discovery, identify, or DNS seeding. The node can no longer learn about new peers. Existing connections are unaffected, but peer rotation and recovery from disconnections are broken. The condition persists until the node is restarted and the in-memory store is cleared (or until enough stored addresses age out and become non-connectable after `ADDR_MAX_RETRIES` failed dial attempts, which requires active dialing of each poisoned address).

---

### Likelihood Explanation

The discovery protocol accepts peer-advertised addresses with no IP-format pre-filter. An attacker operating one or a small number of nodes can advertise batches of unique `/p2p/<random-peer-id>` multiaddreses (which are syntactically valid but have no IP component) across multiple discovery rounds. Filling 16 384 slots requires sending that many unique addresses, which is achievable over time without any special privilege. The attacker does not need majority hashpower, a leaked key, or a Sybil attack — a single persistent peer is sufficient given enough time.

---

### Recommendation

1. **Fix the integer division:** Change `take(len / 2)` to `take(len.saturating_add(1) / 2)` (ceiling division) so that a single-group store still evicts from that group.
2. **Enforce a per-group cap:** Reject or evict addresses that would push any single `Group` (including `Group::None`) beyond a configurable threshold (e.g., 256 entries).
3. **Pre-filter non-socket-parseable addresses:** In `add_addr`, optionally reject addresses that produce `Group::None` unless they are explicitly supported types (e.g., Onion3), preventing the store from being saturated with unroutable entries.

---

### Proof of Concept

```rust
// Fill AddrManager with 16384 unique bare /p2p/<id> addresses (Group::None)
let mut peer_store = PeerStore::default();
for i in 0u64..16384 {
    let mut addr = Multiaddr::empty();
    // construct a unique /p2p/<peer-id> with no IP component
    addr.push(Protocol::P2P(fake_peer_id(i).into()));
    // first 16383 succeed; store fills up
    let _ = peer_store.add_addr(addr, Flags::COMPATIBILITY);
}
// Now attempt to add a legitimate IPv4 peer
let good_addr: Multiaddr = "/ip4/1.2.3.4/tcp/8115".parse().unwrap();
let result = peer_store.add_addr(good_addr, Flags::COMPATIBILITY);
// Asserts Err(EvictionFailed) — legitimate peer is permanently rejected
assert!(result.is_err());
```

The root cause is confirmed at: [7](#0-6)

### Citations

**File:** network/src/peer_store/peer_store_impl.rs (L71-79)
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
```

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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```
