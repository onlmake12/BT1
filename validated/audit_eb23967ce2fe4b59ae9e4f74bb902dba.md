Now I have all the code needed to make a determination. Let me trace through the exact logic.

**`add_addr` creates `AddrInfo` with:**
- `last_connected_at_ms = 0`
- `last_tried_at_ms = 0`
- `attempts_count = 0`

**`is_connectable` for such a freshly-added peer** (types.rs lines 89–104):
- `tried_in_last_minute`: `0 >= now_ms - 60000` → false (now_ms is ~1.7 trillion)
- `last_connected_at_ms == 0 && attempts_count >= ADDR_MAX_RETRIES(3)`: `0 >= 3` → false
- `now_ms - 0 > ADDR_TIMEOUT_MS && attempts_count >= ADDR_MAX_FAILURES(10)`: `0 >= 10` → false
- Returns **`true`**

So all freshly-injected peers are connectable. The first eviction pass finds nothing.

**Second eviction pass** (peer_store_impl.rs lines 358–401):
- Groups by `Group::IP4([a, b])` — the first two octets of the IPv4 address
- 4096 groups × 4 peers = 16384 = `ADDR_COUNT_LIMIT`
- `take(len / 2)` = takes 2048 groups
- For each group: `addrs.len() > 4` → `4 > 4` → **false** → yields `None`
- `candidate_peers` is empty → `Err(PeerStoreError::EvictionFailed)`

The vulnerability is real and the logic flaw is concrete.

---

### Title
Peer store permanently blocked by crafted addr flood with exactly 4 peers per /16 subnet — (`network/src/peer_store/peer_store_impl.rs`)

### Summary
`PeerStore::check_purge` contains a second eviction strategy that only evicts from network groups with **strictly more than 4** peers. An attacker who injects exactly 4 addresses per /16 subnet across 4096 subnets fills the addr store to `ADDR_COUNT_LIMIT` (16384) with no evictable candidates, causing every subsequent `add_addr` call to return `Err(EvictionFailed)` and permanently blocking new peer discovery.

### Finding Description
`ADDR_COUNT_LIMIT` is 16384. [1](#0-0) 

`add_addr` calls `check_purge()` before inserting, propagating any error to the caller. [2](#0-1) 

`check_purge`'s first eviction pass collects only non-connectable peers. Freshly-injected peers created via `AddrInfo::new(addr, 0, score, flags)` have `last_connected_at_ms = 0`, `last_tried_at_ms = 0`, `attempts_count = 0`, and `is_connectable` returns `true` for all of them, so the first pass yields nothing. [3](#0-2) 

The second eviction pass groups peers by `/16` network group, sorts by group size descending, takes the top half, and only evicts from groups where `addrs.len() > 4`: [4](#0-3) 

The `Group::IP4([a, b])` grouping confirms the /16 granularity: [5](#0-4) 

If an attacker injects exactly 4 addresses per /16 subnet across 4096 subnets (4096 × 4 = 16384), every group has size 4. The condition `addrs.len() > 4` is false for all groups, `flat_map` yields `None` for every group, `candidate_peers` is empty, and `Err(PeerStoreError::EvictionFailed)` is returned: [6](#0-5) 

### Impact Explanation
Once the addr store is saturated in this configuration, every call to `add_addr` fails. The node cannot add any new peer addresses from the discovery protocol, effectively halting peer discovery and preventing the node from forming new outbound connections. Existing connections are unaffected, but the node becomes unable to recover connectivity if those connections drop.

### Likelihood Explanation
The CKB discovery protocol allows any peer to advertise arbitrary addresses. A single malicious node can respond to `GetNodes` requests with 16384 fabricated addresses spread across 4096 /16 subnets. No PoW, stake, or privileged role is required. The attacker does not need to own or control those IP ranges — they only need to inject the address strings. The injection can be spread across multiple discovery rounds to avoid any per-message limits.

### Recommendation
Replace the strict `> 4` threshold with `>= 1` (or `> 0`) so that any non-empty group is eligible for eviction when the store is full, or alternatively always evict from the largest group(s) regardless of their size. A secondary fix is to cap the number of addresses accepted per /16 subnet during `add_addr` (e.g., reject a new address if its network group already has ≥ 4 entries and the store is near capacity), which prevents the degenerate distribution from being constructed in the first place. [7](#0-6) 

### Proof of Concept
```rust
// Fill addr_manager with 4096 groups × 4 peers each (all connectable)
let mut peer_store = PeerStore::default();
for a in 0u8..=255 {
    for b in 0u8..=15 {  // 256 * 16 = 4096 subnets
        for c in 1u8..=4 {
            let addr: Multiaddr = format!("/ip4/{}.{}.1.{}/tcp/8115", a, b, c).parse().unwrap();
            // Bypass check_purge by inserting directly
            peer_store.mut_addr_manager().add(AddrInfo::new(addr, 0, 100, Flags::COMPATIBILITY.bits()));
        }
    }
}
assert_eq!(peer_store.addr_manager().count(), 16384);

// Now any add_addr must fail
let new_addr: Multiaddr = "/ip4/200.200.200.200/tcp/8115".parse().unwrap();
let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
assert!(result.is_err(), "Expected EvictionFailed, got Ok");
```

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
