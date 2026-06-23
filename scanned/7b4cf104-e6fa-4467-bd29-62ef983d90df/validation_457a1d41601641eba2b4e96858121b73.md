I have all the code needed to evaluate this claim. Let me trace through the exact logic.

---

## Tracing the Attack Path

### 1. `is_connectable` for Onion3 entries with `attempts_count=0`, `last_connected_at_ms=0` [1](#0-0) 

With `attempts_count=0` and `last_connected_at_ms=0`:
- `tried_in_last_minute`: `last_tried_at_ms=0`, so `0 >= now_ms - 60000` → **false**
- `last_connected_at_ms == 0 && attempts_count >= ADDR_MAX_RETRIES(3)`: `0 >= 3` → **false**
- `attempts_count >= ADDR_MAX_FAILURES(10)`: `0 >= 10` → **false**
- Returns **`true`** (connectable)

So the first pass of `check_purge` finds **zero** non-connectable entries.

### 2. `Group` assignment for Onion3 addresses [2](#0-1) 

Onion3 addresses fail `multiaddr_to_socketaddr`, so they all fall through to `Group::None`. All 16384 entries collapse into a single `Group::None` bucket.

### 3. The `take(len/2)` bug in `check_purge` [3](#0-2) 

With all entries in one group:
- `peers_by_network_group.len()` → `len = 1`
- `peers.into_iter().take(len / 2)` → **`take(0)`** → iterator produces nothing
- `candidate_peers` is empty
- `candidate_peers.is_empty()` → **`return Err(PeerStoreError::EvictionFailed)`**

### 4. Effect on `add_addr` [4](#0-3) 

`add_addr` calls `self.check_purge()?` — the `?` propagates the `Err(EvictionFailed)` to every caller. All subsequent legitimate IPv4/IPv6 peer additions permanently fail.

---

## Verdict

### Title
Peer Store Permanently Blocked by Group::None Flood via Discovery Protocol — (`network/src/peer_store/peer_store_impl.rs`)

### Summary
An unprivileged remote peer can fill all 16384 `ADDR_COUNT_LIMIT` slots with Onion3 (or other non-IP) addresses that resolve to `Group::None`. Once full, `check_purge`'s second-pass `take(len/2)` evaluates to `take(0)` when only one group exists, evicting nothing and permanently returning `Err(EvictionFailed)` for all new `add_addr` calls.

### Finding Description
`check_purge` has two eviction passes:

**Pass 1** — removes non-connectable peers. Onion3 entries with `attempts_count=0` and `last_connected_at_ms=0` are considered connectable (none of the three `is_connectable` rejection conditions trigger), so pass 1 removes nothing.

**Pass 2** — groups by `Group`, sorts by group size descending, then calls `.take(len / 2)`. When all 16384 entries share `Group::None`, `len = 1` and `1 / 2 = 0` in integer arithmetic, so `.take(0)` yields an empty iterator. `candidate_peers` is empty, and the function returns `Err(PeerStoreError::EvictionFailed)`. [5](#0-4) 

The root cause is the `take(len / 2)` expression: it is intended to take the "top half" of groups by population, but when `len = 1`, integer division yields 0 and no group is ever examined.

### Impact Explanation
After the store is saturated, every call to `add_addr` returns `Err`. The node can no longer learn about new honest peers from discovery, cannot refresh stale entries, and is effectively isolated from organic peer discovery for the lifetime of the process (or until the store is manually cleared). This matches the stated Medium scope (suboptimal state causing inability to discover or reconnect to honest peers).

### Likelihood Explanation
The attacker only needs a single P2P connection to the victim. The CKB discovery protocol relays peer addresses; a malicious peer can respond to `GetNodes` with batches of unique Onion3 addresses. 16384 unique Onion3 addresses are trivially generated (the host portion is 10 bytes, giving an enormous address space). No PoW, no privileged role, and no Sybil majority is required — one connection suffices.

### Recommendation
Fix the `take(len / 2)` expression to guarantee at least one group is examined when the store is full:

```rust
// Before
.take(len / 2)

// After: take at least 1 group when the store needs purging
.take((len / 2).max(1))
```

Additionally, consider capping the number of `Group::None` entries accepted (e.g., reject or heavily limit non-IP addresses during `add_addr` when the store is near capacity), and/or rate-limit address advertisements per peer session.

### Proof of Concept
```rust
// Insert 16384 unique Onion3 AddrInfo entries (attempts_count=0, last_connected_at_ms=0)
// then call add_addr with a valid IPv4 addr and assert Ok(()) is returned.
let mut peer_store = PeerStore::default();
for i in 0u64..16384 {
    // Construct unique Onion3 multiaddrs (vary the host bytes)
    let onion_addr: Multiaddr = format!("/onion3/{}:1234", encode_onion_host(i))
        .parse().unwrap();
    peer_store.mut_addr_manager().add(AddrInfo::new(onion_addr, 0, 100, Flags::COMPATIBILITY.bits()));
}
// Now the store is full with Group::None entries
let ipv4_addr: Multiaddr = "/ip4/1.2.3.4/tcp/8115".parse().unwrap();
// This will return Err(EvictionFailed) due to take(0) bug
assert_eq!(peer_store.add_addr(ipv4_addr, Flags::COMPATIBILITY), Ok(()));
```

### Citations

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
