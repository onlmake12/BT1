## Analysis

I've traced the full code path. The vulnerability claim is **valid**. Here is the detailed breakdown.

---

### Title
`check_purge` Integer-Division Zero-Take Allows Peer Store Exhaustion via Single-Group Address Flooding — (`network/src/peer_store/peer_store_impl.rs`)

---

### Summary

`check_purge`'s fallback eviction path uses `take(len / 2)` where `len` is the number of distinct network groups. When all stored addresses belong to the **same /16 group** (`len == 1`), integer division yields `take(0)`, selecting zero candidates for eviction. The function then returns `Err(PeerStoreError::EvictionFailed)`, which propagates out of `add_addr`. An attacker who pre-fills the store with `ADDR_COUNT_LIMIT` (16 384) addresses from one /16 permanently blocks all subsequent peer discovery.

---

### Finding Description

**`ADDR_COUNT_LIMIT` and the two-phase eviction in `check_purge`**

`ADDR_COUNT_LIMIT` is 16 384. [1](#0-0) 

`add_addr` (called by the discovery protocol for every received peer address) calls `check_purge` before inserting: [2](#0-1) 

`check_purge` has two eviction phases:

**Phase 1** — remove addresses where `is_connectable` returns `false`: [3](#0-2) 

**Phase 2** (only entered when Phase 1 found nothing) — group by network segment, sort descending by group size, take the top `len / 2` groups, and from each group with `> 4` members randomly evict 2: [4](#0-3) 

The critical line is:
```rust
peers.into_iter().take(len / 2)
``` [5](#0-4) 

When `len == 1`, `1 / 2 == 0` (integer division), so `take(0)` yields an empty iterator. `candidate_peers` is empty, and the function returns: [6](#0-5) 

**`is_connectable` — why freshly-added addresses survive Phase 1**

A newly added address has `last_connected_at_ms = 0` and `attempts_count = 0`. `is_connectable` only returns `false` when `attempts_count >= ADDR_MAX_RETRIES (3)` with no prior connection, or `attempts_count >= ADDR_MAX_FAILURES (10)` with a stale connection. Fresh addresses pass both checks and are therefore **not** evicted in Phase 1: [7](#0-6) 

**Network group definition — /16 for IPv4**

`Group::IP4` uses only the first two octets, so every address in `1.2.0.0/16` maps to the same group `IP4([1, 2])`: [8](#0-7) 

**`add_outbound_addr` — feeler confirmation sets `last_connected_at_ms = now`**

If the attacker also accepts feeler connections, `add_outbound_addr` stamps `last_connected_at_ms = unix_time_as_millis()`, making the addresses permanently connectable (Phase 1 never evicts them even after the victim retries): [9](#0-8) 

This is triggered from `Feeler::connected` for every successful outbound feeler: [10](#0-9) 

---

### Impact Explanation

Once the store is saturated with 16 384 same-/16 addresses, every subsequent call to `add_addr` returns `EvictionFailed`. The victim node cannot record any new peer addresses learned from the discovery protocol, permanently halting organic peer discovery. The node remains connected only to peers it already knew before the attack, and cannot replace them if they disconnect.

---

### Likelihood Explanation

- The attacker needs only **one** malicious peer already connected to the victim (or reachable via the discovery gossip chain) to inject 16 384 ADDR records.
- All injected addresses can be from a single /16 the attacker controls (or fabricates — discovery addresses are not verified before being stored).
- No PoW, no privileged role, no key material required.
- The feeler-confirmation step (to make addresses permanently connectable) is optional; freshly-added addresses with `attempts_count = 0` already survive Phase 1 eviction.

---

### Recommendation

Replace `take(len / 2)` with `take((len + 1) / 2)` (ceiling division) so that even a single-group store always has at least one group considered for eviction. Additionally, enforce a per-/16 cap when inserting into `addr_manager` (e.g., reject insertion if the group already holds more than `ADDR_COUNT_LIMIT / expected_groups` entries), mirroring Bitcoin Core's `nNew`/`nTried` bucket design.

---

### Proof of Concept

```rust
// Pseudocode — directly exercisable against peer_store_impl
let mut peer_store = PeerStore::default();
let now_ms = ckb_systemtime::unix_time_as_millis();

// Fill store with ADDR_COUNT_LIMIT addresses, all in 1.2.0.0/16
for i in 0..ADDR_COUNT_LIMIT {
    let port = 10000 + i as u16;
    let addr: Multiaddr = format!(
        "/ip4/1.2.{}.{}/tcp/{}/p2p/{}",
        (i / 256) % 256, i % 256, port,
        PeerId::random().to_base58()
    ).parse().unwrap();
    // Stamp last_connected_at_ms = now to survive is_connectable (feeler-confirmed)
    peer_store.add_outbound_addr(addr, Flags::COMPATIBILITY);
}
assert_eq!(peer_store.addr_manager().count(), ADDR_COUNT_LIMIT);

// Now try to add a new honest peer — must fail
let new_addr: Multiaddr = format!(
    "/ip4/8.8.8.8/tcp/8333/p2p/{}", PeerId::random().to_base58()
).parse().unwrap();
let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
assert!(result.is_err()); // EvictionFailed — store is permanently locked
```

The assertion at the end holds because `peers_by_network_group.len() == 1`, `take(1/2) == take(0)`, and `candidate_peers` is empty, causing `check_purge` to return `Err(PeerStoreError::EvictionFailed)`. [11](#0-10)

### Citations

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
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

**File:** network/src/peer_store/peer_store_impl.rs (L327-404)
```rust
    fn check_purge(&mut self) -> Result<()> {
        if self.addr_manager.count() < ADDR_COUNT_LIMIT {
            return Ok(());
        }

        // Evicting invalid data in the peer store is a relatively rare operation
        // There are certain cleanup strategies here:
        // 1. First evict the nodes that have reached the eviction condition
        // 2. If the first step is unsuccessful, enter the network segment grouping mode
        //  2.1. Group current data according to network segment
        //  2.2. Sort according to the amount of data in the same network segment
        //  2.3. In the network segment with more than 4 peer, randomly evict 2 peer

        let now_ms = ckb_systemtime::unix_time_as_millis();
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
        }
        Ok(())
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

**File:** network/src/protocols/feeler.rs (L37-39)
```rust
            self.network_state.with_peer_store_mut(|peer_store| {
                peer_store.add_outbound_addr(session.address.clone(), flags);
            });
```
