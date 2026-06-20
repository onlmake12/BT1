I now have all the code needed to evaluate this claim precisely. Let me trace through every relevant function.

**`check_purge` second pass — the exact arithmetic:** [1](#0-0) 

```
len = peers_by_network_group.len()   // = 1 if all addrs share one /16
take(len / 2)                        // = take(1 / 2) = take(0)  ← integer division
```

Zero groups are iterated → `candidate_peers` is empty → `EvictionFailed` is returned.

**Network group computation confirms a single group for any /16:** [2](#0-1) 

All IPv4 addresses are bucketed by `[octet0, octet1]`, so every address in `X.Y.0.0/16` maps to `Group::IP4([X, Y])` — one group.

**Freshly discovered addresses are always connectable:** [3](#0-2) [4](#0-3) 

`add_addr` creates `AddrInfo` with `last_connected_at_ms=0` and `attempts_count=0`. Since `0 < ADDR_MAX_RETRIES (3)`, `is_connectable` returns `true` for all of them, so the first pass evicts nothing.

**The P2P entrypoint is concrete and unprivileged:** [5](#0-4) 

`DiscoveryAddressManager::add_new_addrs` calls `peer_store.add_addr` for every address received in a `Nodes` discovery message. There is no per-subnet cap. A single `Nodes` message can carry up to 1000 items × 3 addresses = 3000 addresses; ~6 messages fill the 16384-slot store. [6](#0-5) 

**Error is silently swallowed:** [7](#0-6) 

`EvictionFailed` is logged at `debug` level and the address is dropped. The store stays permanently full and no new legitimate peer addresses can ever be added.

---

### Title
`check_purge` integer-division `take(len/2)` evicts nothing when all addresses share one /16 network group, permanently blocking new peer additions — (`network/src/peer_store/peer_store_impl.rs`)

### Summary
`PeerStore::check_purge`'s second-pass eviction uses `take(len / 2)` where `len` is the number of distinct `/16` network groups. When an attacker floods the store with 16 384 addresses from a single `/16` subnet, `len = 1` and integer division yields `take(0)`, iterating zero groups, evicting nothing, and returning `EvictionFailed`. Because all freshly-added addresses are connectable by default, the first pass also evicts nothing. The result is a permanently full peer store that silently rejects every subsequent `add_addr` call.

### Finding Description
`check_purge` in `network/src/peer_store/peer_store_impl.rs` (line 376) computes:

```rust
peers.into_iter().take(len / 2)
```

where `len = peers_by_network_group.len()`. When all 16 384 stored addresses belong to the same `/16` (e.g., `1.2.0.0/16`), `len = 1` and `1 / 2 = 0` in Rust integer arithmetic, so the iterator is immediately exhausted. The inner `addrs.len() > 4` guard is never reached. `candidate_peers` remains empty, and the function returns `Err(PeerStoreError::EvictionFailed)`.

The network-group key is `Group::IP4([octet0, octet1])` (`network/src/network_group.rs` line 28), so any 16 384 distinct addresses within a single `/16` subnet produce exactly one group.

Addresses inserted via `add_addr` are created with `last_connected_at_ms = 0` and `attempts_count = 0`. Because `0 < ADDR_MAX_RETRIES (3)`, `is_connectable` returns `true` for all of them, so the first-pass eviction (which only removes non-connectable addresses) also evicts nothing.

### Impact Explanation
Once the store is saturated, every call to `add_addr` from the discovery protocol returns `EvictionFailed`. The error is silently swallowed at `debug` log level (`discovery/mod.rs` line 355–360). The victim node can no longer learn about any new peers. Its outbound connection pool is limited to the 16 384 attacker-controlled addresses, giving the attacker significant influence over which peers the victim connects to — a prerequisite for eclipse attacks.

### Likelihood Explanation
The attack requires only a single connected peer that sends crafted `Nodes` discovery messages. No PoW, no privileged role, no key material. The discovery protocol imposes no per-subnet rate limit or per-subnet count cap. Filling 16 384 slots requires roughly 6 `Nodes` messages (1 000 items × 3 addresses each = 3 000 addresses per message). Any globally-routable `/16` subnet works (e.g., `1.2.0.0/16`, `8.8.0.0/16`). The attacker does not need to own those IPs; they only need to advertise them.

### Recommendation
Replace `take(len / 2)` with a floor of at least 1:

```rust
.take((len / 2).max(1))
```

Additionally, enforce a per-network-group cap when inserting addresses (e.g., reject insertion if a single `/16` already holds more than N entries), mirroring Bitcoin Core's `addrman` bucketing design.

### Proof of Concept
```rust
let mut peer_store = PeerStore::default();
for i in 0u32..16384 {
    let ip = std::net::Ipv4Addr::from(0x01020000u32 + i); // 1.2.0.0 – 1.2.63.255
    let addr: Multiaddr = format!(
        "/ip4/{}/tcp/8114/p2p/{}",
        ip,
        PeerId::random().to_base58()
    ).parse().unwrap();
    peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
}
// Store is now full with one /16 group; all addresses are connectable.
let new_addr: Multiaddr = format!(
    "/ip4/9.9.9.9/tcp/8114/p2p/{}", PeerId::random().to_base58()
).parse().unwrap();
// This must return EvictionFailed:
assert!(peer_store.add_addr(new_addr, Flags::COMPATIBILITY).is_err());
```

### Citations

**File:** network/src/peer_store/peer_store_impl.rs (L76-79)
```rust
        let score = self.score_config.default_score;
        self.addr_manager
            .add(AddrInfo::new(addr, 0, score, flags.bits()));
        Ok(())
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
