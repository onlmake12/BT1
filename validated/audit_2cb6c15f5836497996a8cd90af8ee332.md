### Title
Peer Store Permanent Lock via Single-Group Address Flooding — (`network/src/peer_store/peer_store_impl.rs`)

### Summary

`check_purge`'s network-group fallback uses integer division `len / 2` to decide how many groups to inspect. When all stored addresses belong to a **single** network group (`len = 1`), `1 / 2 = 0`, so `take(0)` yields an empty iterator, zero addresses are evicted, and the function returns `Err(PeerStoreError::EvictionFailed)`. Because `add_addr` propagates that error with `?`, the peer store becomes permanently locked: no address from any network group can ever be added again.

---

### Finding Description

**`check_purge` — step-by-step trace**

`check_purge` is called on every `add_addr` invocation once `addr_manager.count() >= ADDR_COUNT_LIMIT` (16 384). [1](#0-0) 

**Step 1 — connectable filter.** Freshly injected addresses have `last_connected_at_ms = 0`, `attempts_count = 0`, `last_tried_at_ms = 0`. Walking `is_connectable`: [2](#0-1) 

- `tried_in_last_minute`: `0 >= now_ms − 60 000` → **false** (current epoch-ms is ~1.7 × 10¹²).
- `last_connected_at_ms == 0 && attempts_count >= 3`: `0 >= 3` → **false**.
- `now_ms − 0 > ADDR_TIMEOUT_MS (7 days) && attempts_count >= 10`: second clause `0 >= 10` → **false**.

Result: every freshly injected address is "connectable"; step 1 evicts **nothing**.

**Step 2 — network-group fallback.** [3](#0-2) 

```
let len = peers_by_network_group.len();   // = 1 (all same /16)
peers.into_iter()
    .take(len / 2)                        // take(0) — empty!
    .flat_map(|addrs| { … })
    .collect()                            // candidate_peers = []
```

`candidate_peers` is empty → `return Err(PeerStoreError::EvictionFailed)`. [4](#0-3) 

`add_addr` propagates this error: [5](#0-4) 

**Network-group key.** IPv4 addresses are bucketed by the first two octets only, so every address in `1.2.0.0/16` maps to `Group::IP4([1, 2])` — a single group. [6](#0-5) 

**Attacker entry point.** Addresses reach `add_addr` via the P2P discovery protocol (`network/src/protocols/discovery/mod.rs`). There is no per-group cap or per-sender rate limit inside `add_addr` itself; the only guard is the global `ADDR_COUNT_LIMIT = 16 384`. [7](#0-6) 

A /16 subnet contains 65 536 addresses, so supplying 16 384 distinct addresses from one /16 is trivially feasible.

---

### Impact Explanation

Once the store is locked, every subsequent `add_addr` call returns `Err(EvictionFailed)`. The node cannot record any new peer address — from any network group — until it is restarted (at which point the attacker can repeat the flood). Over time, as existing connections drop, the node cannot replenish its peer set and becomes progressively isolated from the network.

---

### Likelihood Explanation

The attack requires only that the adversary can deliver ~16 384 `GetAddr`/`SendAddr` discovery messages carrying addresses from a single /16 subnet. This is achievable from a single controlled node or a small botnet. No PoW, no key material, and no privileged access is needed. The bug is deterministic and reproducible in a local test environment.

---

### Recommendation

Replace the `len / 2` integer division with a minimum-of-one guard, and add a hard per-group cap during insertion:

```rust
// In check_purge fallback:
let take_count = (len / 2).max(1);
peers.into_iter().take(take_count) …

// In add_addr (or AddrManager::add):
const MAX_ADDRS_PER_GROUP: usize = 256;
if group_count >= MAX_ADDRS_PER_GROUP { return Ok(()); }
```

---

### Proof of Concept

```rust
// Fill store with 16384 addresses from 1.2.x.x
let mut peer_store = PeerStore::default();
for i in 0u16..=255 {
    for j in 0u16..=63 {
        let addr: Multiaddr = format!("/ip4/1.2.{i}.{j}/tcp/8115").parse().unwrap();
        peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
}
// Store is now at ADDR_COUNT_LIMIT with 1 network group
// Any subsequent add_addr — even from a completely different /16 — must fail
let new_addr: Multiaddr = "/ip4/9.9.9.9/tcp/8115".parse().unwrap();
assert!(peer_store.add_addr(new_addr, Flags::COMPATIBILITY).is_err());
```

The assertion passes because `check_purge` computes `len=1`, `len/2=0`, evicts nothing, and returns `Err(EvictionFailed)`. [8](#0-7)

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

**File:** network/src/peer_store/peer_store_impl.rs (L327-330)
```rust
    fn check_purge(&mut self) -> Result<()> {
        if self.addr_manager.count() < ADDR_COUNT_LIMIT {
            return Ok(());
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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```
