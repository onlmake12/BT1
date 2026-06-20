### Title
Peer Store Eviction Deadlock via Crafted Address Distribution — (`network/src/peer_store/peer_store_impl.rs`)

### Summary

`PeerStore::check_purge` has two sequential eviction strategies. Both can be simultaneously defeated by an attacker who advertises exactly `ADDR_COUNT_LIMIT` (16 384) addresses distributed across ≥ 4 096 distinct /16 network groups with ≤ 4 addresses per group. When this condition holds, `check_purge` returns `Err(EvictionFailed)`, causing every subsequent `add_addr` call to propagate that error and permanently block new peer discovery until the node organically retries and fails enough connections to the fake addresses.

---

### Finding Description

**Strategy 1 — "evict non-connectable" (lines 341–355):**

`check_purge` collects every address for which `is_connectable` returns `false`. [1](#0-0) 

`is_connectable` returns `true` for a freshly-added address because `AddrInfo::new` initialises `last_tried_at_ms = 0` and `attempts_count = 0`. [2](#0-1) 

With `attempts_count = 0 < ADDR_MAX_RETRIES (3)` and `last_connected_at_ms = 0`, neither non-connectable branch fires: [3](#0-2) 

So strategy 1 finds zero candidates and falls through.

---

**Strategy 2 — "evict from over-represented groups" (lines 358–401):** [4](#0-3) 

The algorithm:
1. Groups all addresses by `/16` network group.
2. Sorts groups by descending size.
3. Takes only the **top `len/2` groups** (`peers.take(len / 2)`).
4. Within those groups, evicts 2 addresses only if `addrs.len() > 4`.

If the attacker fills the store with 16 384 entries spread across **4 096 distinct /16 groups of exactly 4 each**:
- `len = 4096`, `len/2 = 2048`
- Every group in the top 2048 has exactly 4 entries — the `> 4` guard is never satisfied
- `candidate_peers` is empty → `Err(EvictionFailed)` is returned [5](#0-4) 

---

**Entry point — discovery protocol:**

`add_addr` is called directly from the discovery protocol handler (`network/src/protocols/discovery/mod.rs`) for every peer address received over P2P. The only guard is a ban-list check; there is no rate-limit or reachability verification before the address is inserted. [6](#0-5) 

An attacker with a single P2P connection can send discovery messages advertising 16 384 syntactically valid but unreachable IP addresses drawn from 4 096+ distinct /16 subnets. No actual ownership of those IPs is required.

---

### Impact Explanation

Once `check_purge` returns `Err(EvictionFailed)`, every call to `add_addr` propagates that error. The node cannot admit any new peer address from honest nodes, breaking peer discovery. The node retains its existing connections but cannot grow its peer set or replace lost peers, leading to progressive network isolation.

The attack is **self-limiting but slow to recover**: the node must organically attempt connections to each of the 16 384 fake addresses and fail 3 times each (`ADDR_MAX_RETRIES = 3`) before strategy 1 can evict them. [7](#0-6) 

---

### Likelihood Explanation

- Requires only one P2P connection to the victim.
- Generating 16 384 fake IP addresses across 4 096 /16 subnets is trivial (random address generation).
- No PoW, no stake, no privileged role required.
- The discovery protocol imposes no per-session address count limit visible in the production code.

---

### Recommendation

1. **Fix the `> 4` guard in strategy 2** to `>= 4` (or `> 1`), so groups at the limit are also eligible for eviction.
2. **Cap addresses per /16 group at insertion time** in `add_addr`, rejecting the new address if the group already holds ≥ 4 entries and the store is full.
3. **Rate-limit addresses accepted per peer session** in the discovery handler.
4. **Add a fallback eviction** that randomly evicts any address when both strategies fail, rather than returning `Err`.

---

### Proof of Concept

```rust
// Pseudocode — fill peer store with 4096 groups × 4 addrs each
let mut peer_store = PeerStore::default();
for group in 0u16..4096 {
    for host in 1u8..=4 {
        // Each group is a distinct /16: 1.{group_hi}.{group_lo}.{host}
        let ip = format!("{}.{}.{}.{}",
            1, (group >> 8) as u8, group as u8, host);
        let addr: Multiaddr = format!("/ip4/{}/tcp/8115", ip).parse().unwrap();
        peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
}
// Store is now at ADDR_COUNT_LIMIT (16384) with 4096 groups of 4
// All entries: last_tried_at_ms=0, attempts_count=0 → is_connectable=true
// Strategy 1: no non-connectable entries → skipped
// Strategy 2: all groups have exactly 4 entries, none > 4 → no eviction

let new_addr: Multiaddr = "/ip4/200.0.0.1/tcp/8115".parse().unwrap();
let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
assert!(result.is_err()); // Err(EvictionFailed) — peer discovery blocked
``` [8](#0-7) [9](#0-8)

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

**File:** network/src/peer_store/mod.rs (L26-35)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
/// Consider we never seen a peer if peer's last_connected_at beyond this timeout
const ADDR_TIMEOUT_MS: u64 = 7 * 24 * 3600 * 1000;
/// The timeout that peer's address should be added to the feeler list again
pub(crate) const ADDR_TRY_TIMEOUT_MS: u64 = 3 * 24 * 3600 * 1000;
/// When obtaining the list of selectable nodes for identify,
/// the node that has just been disconnected needs to be excluded
pub(crate) const DIAL_INTERVAL: u64 = 15 * 1000;
const ADDR_MAX_RETRIES: u32 = 3;
const ADDR_MAX_FAILURES: u32 = 10;
```
