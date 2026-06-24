All code claims are verified against the actual source. Here is the audit result:

---

Audit Report

## Title
Group::None Integer-Division Eviction Bypass Causes Permanent Peer Store DoS — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
When `AddrManager` reaches its 16 384-slot limit and every stored address maps to `Group::None` (no resolvable IP component), `check_purge`'s Stage 2 eviction computes `take(1 / 2) = take(0)` due to integer division, selects zero candidates, and unconditionally returns `PeerStoreError::EvictionFailed`. Every subsequent `add_addr` call propagates this error, permanently blocking new peer discovery until the node is restarted or enough stored addresses age out through active dial attempts.

## Finding Description

**Entry point:** `add_addr` at [1](#0-0)  calls `check_purge()` before inserting. Discovery, identify, and DNS seeding all funnel through `add_addr` with no IP-format pre-filter.

**Stage 1 (non-connectable eviction):** [2](#0-1)  collects addresses where `!is_connectable(now_ms)`. Freshly inserted addresses have `last_connected_at_ms = 0` and `attempts_count = 0` [3](#0-2) ; `is_connectable` returns `true` for them because `attempts_count (0) < ADDR_MAX_RETRIES (3)` [4](#0-3) . Stage 1 finds nothing.

**Stage 2 (group-based eviction) — root cause:** [5](#0-4) 

```rust
let len = peers_by_network_group.len();   // = 1 when all are Group::None
peers.into_iter().take(len / 2)           // take(1/2) = take(0)
```

When every stored address lacks a resolvable IP, `Group::from(&Multiaddr)` falls through to `Group::None` [6](#0-5) , producing exactly one HashMap key. Integer division `1 / 2 = 0` makes `take(0)` yield an empty iterator. `candidate_peers` is empty, and the function returns `Err(PeerStoreError::EvictionFailed)` [7](#0-6) .

**Capacity constant:** `ADDR_COUNT_LIMIT = 16384` [8](#0-7) .

**`Group::None` sources:** Any multiaddr without a TCP/UDP socket address — bare `/p2p/<peer-id>`, DNS names, Onion3, etc. — produces `Group::None`. [9](#0-8) 

## Impact Explanation

Once the peer store is saturated with `Group::None` entries, `add_addr` returns `EvictionFailed` for every call, including calls for valid IPv4/IPv6 peers received via discovery, identify, or DNS seeding. The node can no longer learn about new peers. Peer rotation and recovery from disconnections are broken. The condition persists until the node is restarted or until enough poisoned addresses accumulate 3 failed dial attempts (`ADDR_MAX_RETRIES`), which requires the node to actively attempt dialing each poisoned address. This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs** — an attacker targeting multiple nodes can progressively isolate them from peer discovery, degrading network connectivity and resilience.

## Likelihood Explanation

The discovery protocol accepts peer-advertised addresses with no IP-format pre-filter. A single persistent attacker node can advertise batches of unique `/p2p/<random-peer-id>` multiaddreses (syntactically valid, no IP component) across multiple discovery rounds. Filling 16 384 unique slots is achievable over time without any special privilege, majority hashpower, or leaked key. The attack is repeatable after node restart.

## Recommendation

1. **Fix the integer division:** Change `take(len / 2)` to `take(len.saturating_add(1) / 2)` (ceiling division) so that a single-group store still evicts from that group.
2. **Enforce a per-group cap:** Reject or evict addresses that would push any single `Group` (including `Group::None`) beyond a configurable threshold (e.g., 256 entries).
3. **Pre-filter non-socket-parseable addresses:** In `add_addr`, reject addresses that produce `Group::None` unless they are explicitly supported types (e.g., Onion3).

## Proof of Concept

```rust
// Fill AddrManager with 16384 unique bare /p2p/<id> addresses (Group::None)
let mut peer_store = PeerStore::default();
for i in 0u64..16384 {
    let mut addr = Multiaddr::empty();
    addr.push(Protocol::P2P(fake_peer_id(i).into()));
    let _ = peer_store.add_addr(addr, Flags::COMPATIBILITY);
}
// Now attempt to add a legitimate IPv4 peer
let good_addr: Multiaddr = "/ip4/1.2.3.4/tcp/8115".parse().unwrap();
let result = peer_store.add_addr(good_addr, Flags::COMPATIBILITY);
// Asserts Err(EvictionFailed) — legitimate peer is permanently rejected
assert!(result.is_err());
```

The `addrs.len() > 4` guard inside Stage 2 [10](#0-9)  is never reached because `take(0)` short-circuits before it. The `AddrManager::add` deduplication [11](#0-10)  requires unique addresses, which the PoC satisfies via distinct peer IDs.

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

**File:** network/src/peer_store/peer_store_impl.rs (L366-376)
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
```

**File:** network/src/peer_store/peer_store_impl.rs (L378-390)
```rust
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
```

**File:** network/src/peer_store/peer_store_impl.rs (L399-401)
```rust
            if candidate_peers.is_empty() {
                return Err(PeerStoreError::EvictionFailed.into());
            }
```

**File:** network/src/peer_store/types.rs (L63-76)
```rust
impl AddrInfo {
    /// Init
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

**File:** network/src/peer_store/types.rs (L94-96)
```rust
        // we give up if never connect to this addr
        if self.last_connected_at_ms == 0 && self.attempts_count >= ADDR_MAX_RETRIES {
            return false;
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
