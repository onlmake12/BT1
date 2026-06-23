Now I have all the code needed to fully trace the vulnerability claim. Let me analyze each component precisely.

### Title
Peer Store Eviction Bypass via Single-Group Address Flood — (`network/src/peer_store/peer_store_impl.rs`)

### Summary
`check_purge`'s second-pass eviction uses `len / 2` (integer division) to decide how many network groups to evict from. When all stored addresses belong to a single network group, `len = 1` and `1 / 2 = 0`, so `.take(0)` processes zero groups, produces an empty candidate list, and returns `EvictionFailed`. An attacker who pre-fills the store with 16 384 connectable addresses from a single /16 subnet can trigger this path on every subsequent `add_addr` call, blocking peer discovery address ingestion.

---

### Finding Description

**Entrypoint — Discovery protocol:**

`add_new_addrs` in `network/src/protocols/discovery/mod.rs` is called for every inbound Discovery Nodes message. It iterates the received addresses and calls `peer_store.add_addr(addr, flags)` for each one that passes `is_valid_addr`. [1](#0-0) 

**`add_addr` always calls `check_purge` first:** [2](#0-1) 

**`is_connectable()` returns `true` for every freshly-added address:**

`add_addr` constructs `AddrInfo::new(addr, 0, score, flags)`, which sets `last_connected_at_ms=0`, `attempts_count=0`, `last_tried_at_ms=0`. Walking through `is_connectable`:

- `tried_in_last_minute`: `0 >= now_ms − 60 000` → **false** (now_ms ≈ 1.7 × 10¹² ms)
- `last_connected_at_ms == 0 && attempts_count >= ADDR_MAX_RETRIES(3)`: `0 >= 3` → **false**
- third clause: `now_ms − 0 > ADDR_TIMEOUT_MS` is true, but `attempts_count >= ADDR_MAX_FAILURES(10)`: `0 >= 10` → **false**

Result: `is_connectable()` = **true**. [3](#0-2) 

**First pass of `check_purge` finds nothing to evict:**

All 16 384 entries are connectable, so `candidate_peers` is empty and no removals occur. [4](#0-3) 

**Second pass: the `len / 2` integer-division bug:**

Network group is keyed by the first two octets of IPv4 (`Group::IP4([bits[0], bits[1]])`), i.e. a /16. [5](#0-4) 

If all addresses share one /16, `peers_by_network_group.len() = 1`. Then:

```
let len = 1;
peers.into_iter().take(len / 2)   // take(0) — processes zero groups
```

`candidate_peers` is again empty → `return Err(PeerStoreError::EvictionFailed)`. [6](#0-5) 

**`is_valid_addr` does not block the attack:**

The filter rejects non-globally-reachable IPs (private ranges), but the attacker can use any public /16 block (e.g. `1.2.0.0/16`). 16 384 distinct ports or peer IDs across 65 536 host addresses in that /16 are trivially constructable.

---

### Impact Explanation

Every `add_addr` call after the store is full returns `EvictionFailed`. The Discovery protocol silently drops the error (debug log only), so the node stops learning new peer addresses from Discovery messages. Peer discovery is degraded for the lifetime of the flooded entries.

**Important correction to the question's "permanent" claim:** the lock is *not* permanent. The node's feeler mechanism will attempt connections to the flooded addresses. After `ADDR_MAX_RETRIES = 3` failed attempts each, `is_connectable()` returns `false` and they become evictable in the first pass. The degradation window is bounded by how quickly the feeler exhausts 16 384 addresses. [7](#0-6) 

`add_outbound_addr` and `add_connected_peer` bypass `check_purge` entirely, so outbound dialing and connection tracking are unaffected. [8](#0-7) 

---

### Likelihood Explanation

- A single connected peer can send arbitrarily many Discovery Nodes messages; no per-sender rate limit is enforced in `add_new_addrs`.
- The attacker only needs one P2P connection to a fresh or lightly-populated node.
- Constructing 16 384 valid multiaddrs across a public /16 is trivial.

---

### Recommendation

Replace `take(len / 2)` with a guard that always evicts at least one group when the store is full:

```rust
let take_count = if len == 1 { 1 } else { len / 2 };
peers.into_iter().take(take_count)...
```

Additionally, enforce a per-network-group cap in `add_addr` (e.g. reject if the group already holds more than `ADDR_COUNT_LIMIT / expected_groups` entries) to prevent the store from being monopolised by a single /16 in the first place.

---

### Proof of Concept

```rust
// Fill addr_manager with ADDR_COUNT_LIMIT addresses, all from 1.2.x.x (/16 group IP4([1,2]))
let mut peer_store = PeerStore::default();
for i in 0..16384u32 {
    let port = (i % 65535) + 1;
    let host_a = (i / 65535) as u8;
    let addr: Multiaddr = format!(
        "/ip4/1.2.{}.{}/tcp/{}/p2p/{}",
        host_a, i as u8, port,
        PeerId::random().to_base58()
    ).parse().unwrap();
    // bypass check_purge by inserting directly
    peer_store.mut_addr_manager().add(AddrInfo::new(addr, 0, 100, Flags::COMPATIBILITY.bits()));
}
assert_eq!(peer_store.addr_manager().count(), 16384);

// Now add_addr must call check_purge; all entries are connectable, single group → EvictionFailed
let new_addr: Multiaddr = "/ip4/1.2.100.100/tcp/9999/p2p/...".parse().unwrap();
let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
assert!(result.is_err()); // PeerStoreError::EvictionFailed
```

### Citations

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

**File:** network/src/peer_store/peer_store_impl.rs (L103-114)
```rust
    pub fn add_outbound_addr(&mut self, addr: Multiaddr, flags: Flags) {
        if self.ban_list.is_addr_banned(&addr) {
            return;
        }
        let score = self.score_config.default_score;
        self.addr_manager.add(AddrInfo::new(
            addr,
            ckb_systemtime::unix_time_as_millis(),
            score,
            flags.bits(),
        ));
    }
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

**File:** network/src/peer_store/peer_store_impl.rs (L366-401)
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

**File:** network/src/peer_store/mod.rs (L34-35)
```rust
const ADDR_MAX_RETRIES: u32 = 3;
const ADDR_MAX_FAILURES: u32 = 10;
```
