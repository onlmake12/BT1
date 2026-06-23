The code is fully readable. Let me trace the exact logic.

**`check_purge` second eviction path — `take(len / 2)` with `len == 1`**

When all `ADDR_COUNT_LIMIT` (16384) stored addresses share the same /16 subnet, they all map to a single `Group::IP4([a, b])` key. The second eviction path computes:

```rust
let len = peers_by_network_group.len();  // == 1
// ...
peers.into_iter().take(len / 2)          // take(1/2) == take(0)
```

`take(0)` produces an empty iterator, `candidate_peers` is empty, and the function returns `Err(PeerStoreError::EvictionFailed)`. [1](#0-0) 

`add_addr` propagates this error with `?`, permanently blocking all subsequent callers. [2](#0-1) 

`ADDR_COUNT_LIMIT` is exactly 16384. [3](#0-2) 

A freshly advertised address has `last_connected_at_ms = 0` and `attempts_count = 0`. Since `0 < ADDR_MAX_RETRIES`, `is_connectable` returns `true`, so step 1 (non-connectable eviction) never fires. [4](#0-3) 

The `Group::IP4` key is derived from only the first two octets, so all of `225.0.0.0/16` maps to `Group::IP4([225, 0])`. [5](#0-4) 

The attack is reachable via the P2P discovery protocol: a single malicious peer can advertise up to 16384 addresses (all within one /16 subnet) across multiple discovery messages, filling the peer store. After that, every honest peer's `add_addr` call returns `Err(EvictionFailed)`.

---

### Title
`check_purge` `take(len/2)` integer-division zero-take permanently blocks peer store writes when all addresses share one /16 subnet — (`network/src/peer_store/peer_store_impl.rs`)

### Summary
When `peers_by_network_group` contains exactly one group (all stored addresses share the same /16 subnet), `take(len / 2)` evaluates to `take(0)`, producing an empty candidate list. `check_purge` returns `PeerStoreError::EvictionFailed`, and `add_addr` propagates the error, permanently denying peer store writes to all callers.

### Finding Description
`check_purge` has two eviction stages:

1. **Stage 1** — remove non-connectable addresses. Freshly advertised addresses (`last_connected_at_ms = 0`, `attempts_count = 0`) are always connectable, so this stage produces nothing.
2. **Stage 2** — group by `/16` network segment, sort by group size descending, `take(len / 2)` of the largest groups, evict 2 random peers from any group with `> 4` members.

The bug is in stage 2: when all 16384 addresses belong to one group, `len = 1` and `1 / 2 = 0` (integer division), so `take(0)` processes zero groups. `candidate_peers` is empty, and the function returns `Err(PeerStoreError::EvictionFailed)`. [6](#0-5) 

### Impact Explanation
The peer store is permanently full. No new peer addresses can be added via `add_addr` (used by the discovery protocol). The node cannot discover or connect to new honest peers, effectively isolating it from the network. The impact is a **persistent peer discovery denial** achievable by a single unauthenticated remote peer.

### Likelihood Explanation
The discovery protocol allows any connected peer to advertise arbitrary IP addresses. An attacker needs to advertise exactly 16384 addresses within the same /16 subnet (e.g., `225.0.0.0–225.0.255.255`). This requires no special privilege, no PoW, and no key material. The condition is stable: once the store is full with same-group connectable addresses, it stays blocked indefinitely.

### Recommendation
Replace `take(len / 2)` with a formulation that always processes at least one group when `len >= 1`, for example `take(len.saturating_add(1) / 2)` (ceiling division), or `take(len / 2).max(1)`. Additionally, consider capping the number of addresses accepted per /16 group during insertion to prevent a single subnet from monopolizing the store. [7](#0-6) 

### Proof of Concept
```rust
// Fill peer store with ADDR_COUNT_LIMIT connectable addresses, all in 225.0.x.x
let mut peer_store = PeerStore::default();
for i in 0..ADDR_COUNT_LIMIT {
    let addr: Multiaddr = format!(
        "/ip4/225.0.{}.{}/tcp/1000/p2p/{}",
        (i / 256) % 256, i % 256,
        PeerId::random().to_base58()
    ).parse().unwrap();
    peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
}
assert_eq!(peer_store.addr_manager().count(), ADDR_COUNT_LIMIT);

// Now try to add an honest peer from a different subnet
let honest: Multiaddr = format!(
    "/ip4/192.168.1.1/tcp/1000/p2p/{}", PeerId::random().to_base58()
).parse().unwrap();
let result = peer_store.add_addr(honest, Flags::COMPATIBILITY);

// Demonstrates the bug: EvictionFailed, count unchanged, honest peer rejected
assert!(result.is_err());
assert_eq!(peer_store.addr_manager().count(), ADDR_COUNT_LIMIT);
```

### Citations

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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
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
