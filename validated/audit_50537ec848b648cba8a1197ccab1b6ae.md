Now I have all the information needed to analyze this vulnerability claim thoroughly. Let me trace through the exact code paths.

### Title
Peer Store Eviction Bypass via Distinct /16 Subnet Flooding Causes Permanent Discovery Starvation — (`network/src/peer_store/peer_store_impl.rs`)

---

### Summary

`check_purge` has two eviction passes. An unprivileged remote peer can craft discovery `Nodes` messages that fill the peer store with 16384 addresses, each from a distinct /16 subnet, making both passes produce zero eviction candidates and return `Err(EvictionFailed)`. All subsequent `add_addr` calls from honest discovery messages are then silently dropped, permanently blocking peer discovery.

---

### Finding Description

**Entrypoint:** A connected peer sends a discovery `Nodes` (announce=false) message. The protocol handler calls `add_new_addrs` → `add_addr` → `check_purge`.

**`check_purge` logic — two passes:**

**Pass 1 — evict non-connectable addresses:** [1](#0-0) 

This collects addresses where `is_connectable(now_ms)` returns `false`. For every address inserted via `add_addr`, `AddrInfo::new` hard-codes `last_connected_at_ms = 0` and `attempts_count = 0`: [2](#0-1) [3](#0-2) 

With those defaults, `is_connectable` evaluates as: [4](#0-3) 

- `tried_in_last_minute`: false (`last_tried_at_ms = 0`)
- `last_connected_at_ms == 0 && attempts_count >= ADDR_MAX_RETRIES(3)`: false (0 < 3)
- `now_ms.saturating_sub(0) > ADDR_TIMEOUT_MS && attempts_count >= ADDR_MAX_FAILURES(10)`: first clause true, second false (0 < 10)

Result: **`is_connectable` returns `true`** for every freshly-added address. Pass 1 evicts nothing.

**Pass 2 — evict from over-represented network groups:** [5](#0-4) 

Groups are keyed by `/16` subnet (`Group::IP4([bits[0], bits[1]])`): [6](#0-5) 

The pass only considers the top `len/2` groups by size, and within those only evicts from groups with `> 4` peers. If the attacker places exactly 1 address per /16 subnet across 16384 distinct subnets, every group has size 1. The condition `addrs.len() > 4` is never true. Pass 2 also evicts nothing.

**Result:** [7](#0-6) 

`Err(PeerStoreError::EvictionFailed)` propagates back to `add_new_addrs`, which silently logs it at `debug` level and discards the new address: [8](#0-7) 

**Flooding feasibility:** A non-announce `Nodes` message may carry up to `MAX_ADDR_TO_SEND = 1000` items × `MAX_ADDRS = 3` addresses = 3000 addresses per message: [9](#0-8) [10](#0-9) 

Six such messages fill the 16384-slot store. IPv4 has 65536 possible /16 subnets, so 16384 distinct subnets is trivially achievable.

**Sustained attack:** As the node attempts outbound connections to fake addresses and fails, `attempts_count` increments. After 3 failures with `last_connected_at_ms == 0`, `is_connectable` returns false (`ADDR_MAX_RETRIES = 3`), and those slots are reclaimed. The attacker simply resends discovery messages to refill them, maintaining the full store indefinitely with a single persistent connection. [11](#0-10) 

---

### Impact Explanation

The victim node's peer store is permanently saturated with attacker-controlled fake addresses. All honest discovery `add_addr` calls return `Err` and are silently dropped. The node cannot learn about new peers, cannot replenish its outbound connection pool as peers churn, and eventually loses the ability to find sync peers — causing peer discovery starvation.

---

### Likelihood Explanation

The attack requires only a single inbound or outbound P2P connection and approximately 6 discovery messages. No special privileges, no PoW, no key material. The attacker can maintain the attack indefinitely with minimal bandwidth by periodically resending messages to replace evicted slots. Any node reachable on the public network is exposed.

---

### Recommendation

1. **Cap per-session address contributions:** Limit how many addresses a single peer session can insert into the peer store (e.g., 100–500 per session).
2. **Enforce group density at insertion time:** Before inserting a new address, check whether its /16 group already has ≥ N entries and reject if so, rather than deferring to eviction.
3. **Unconditional eviction fallback:** When both passes fail, forcibly evict the oldest-tried or lowest-scored address regardless of group size, so `check_purge` never returns `Err`.
4. **Rate-limit discovery message processing** per session to slow the fill rate.

---

### Proof of Concept

```rust
// Pseudocode — locally testable unit test
let mut peer_store = PeerStore::default();

// Fill with 16384 addresses, one per distinct /16 subnet
for i in 0u32..16384 {
    let a = ((i >> 8) & 0xFF) as u8;
    let b = (i & 0xFF) as u8;
    let addr: Multiaddr = format!(
        "/ip4/{}.{}.0.1/tcp/8114/p2p/{}",
        a, b, PeerId::random().to_base58()
    ).parse().unwrap();
    // Simulates what add_new_addrs does via discovery
    peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
}
assert_eq!(peer_store.addr_manager().count(), 16384);

// Now attempt to add an honest peer's address
let honest_addr: Multiaddr = format!(
    "/ip4/200.100.50.1/tcp/8114/p2p/{}", PeerId::random().to_base58()
).parse().unwrap();
let result = peer_store.add_addr(honest_addr.clone(), Flags::COMPATIBILITY);

// Assertion: returns Err, honest address not stored
assert!(result.is_err());
assert!(peer_store.addr_manager().get(&honest_addr).is_none());
```

### Citations

**File:** network/src/peer_store/peer_store_impl.rs (L76-79)
```rust
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

**File:** network/src/peer_store/types.rs (L88-105)
```rust
    /// Whether connectable peer
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

**File:** network/src/protocols/discovery/mod.rs (L32-34)
```rust
const MAX_ADDR_TO_SEND: usize = 1000;
// The maximum number addresses in one Nodes item
const MAX_ADDRS: usize = 3;
```

**File:** network/src/protocols/discovery/mod.rs (L279-288)
```rust
    } else if nodes.items.len() > MAX_ADDR_TO_SEND {
        warn!(
            "Too many items (announce=false) length={}",
            nodes.items.len()
        );
        misbehavior = Some(Misbehavior::TooManyItems {
            announce: nodes.announce,
            length: nodes.items.len(),
        });
    }
```

**File:** network/src/protocols/discovery/mod.rs (L354-362)
```rust
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
