The code is all present. Let me trace the exact logic.

**Step 1 — Onion3 → `Group::None`**

In `network_group.rs`, `From<&Multiaddr> for Group` calls `multiaddr_to_socketaddr`. Onion3 addresses cannot be converted to a socket address, so that call returns `None`, and the function falls through to the final `Group::None` return. [1](#0-0) 

**Step 2 — `check_purge` eviction path**

`check_purge` is called by `add_addr` on every new address insertion once the store reaches `ADDR_COUNT_LIMIT` (16384). [2](#0-1) [3](#0-2) 

The function has two eviction passes:

1. **First pass** — removes addresses where `!addr.is_connectable(now_ms)`. Freshly advertised addresses (added with `last_connected_at_ms=0`, default score, zero attempts) are connectable, so this pass removes nothing. [4](#0-3) 

2. **Second pass** — groups by `Group`, takes `len / 2` of the largest groups, and evicts 2 from each group with `> 4` entries. [5](#0-4) 

**Step 3 — The integer-division bug**

When all 16384 entries are Onion3, `peers_by_network_group` has exactly **one** key: `Group::None`. Therefore:

```
len = 1
take(len / 2) = take(0)   // integer division: 1/2 = 0
```

`take(0)` produces an empty iterator. `candidate_peers` is empty. The function returns:

```rust
return Err(PeerStoreError::EvictionFailed.into());
``` [6](#0-5) 

**Step 4 — Reachability**

The CKB discovery protocol allows any connected peer to advertise addresses via P2P messages. `add_addr` is the handler for those advertisements. An attacker with a single connection can relay Onion3 addresses across multiple discovery messages until the store is saturated. No privilege, PoW, or key is required.

**Step 5 — Impact**

Once `check_purge` returns `Err`, `add_addr` propagates the error and the new address is rejected. Every subsequent attempt to add any address (regardless of type) will also fail because the store remains at capacity and the eviction logic is permanently broken for a homogeneous `Group::None` store. The node loses the ability to discover new peers. If existing connections drop, the node cannot replenish them and risks network isolation.

---

### Title
Peer store permanently blocked by Onion3 address flooding via `Group::None` eviction collapse — (`network/src/peer_store/peer_store_impl.rs`)

### Summary
`check_purge`'s network-group eviction path uses `take(len / 2)` where `len` is the number of distinct groups. When all stored addresses are Onion3 (which all hash to `Group::None`), `len = 1` and integer division yields `take(0)`, producing an empty eviction candidate list and returning `Err(EvictionFailed)`. This permanently prevents any new address from being admitted to the peer store.

### Finding Description
`Group::None` is a single hash-map key shared by every address type that `multiaddr_to_socketaddr` cannot resolve — including all Onion3 addresses. An attacker who fills the 16384-slot peer store exclusively with Onion3 addresses causes the second eviction pass to build a map with exactly one entry. The `take(len / 2)` call with `len = 1` evaluates to `take(0)`, so no candidates are selected, no entries are removed, and `EvictionFailed` is returned unconditionally on every subsequent `add_addr` call.

### Impact Explanation
The peer store is the sole source of addresses for outbound connection attempts and feeler connections. With `add_addr` permanently failing, the node cannot learn about new peers. If existing connections are lost (peer restart, NAT churn, etc.), the node cannot reconnect and becomes isolated from the network, unable to receive blocks or transactions.

### Likelihood Explanation
Any peer connected to the victim node can advertise Onion3 addresses via the discovery protocol. No authentication, hashpower, or special privilege is required. The attacker needs only to send enough discovery messages to fill 16384 slots, which is achievable from a single connection over a short period.

### Recommendation
Replace `take(len / 2)` with a minimum of 1:

```rust
peers.into_iter()
    .take(std::cmp::max(1, len / 2))
    ...
```

Additionally, consider capping the number of `Group::None` addresses accepted (e.g., treat them as a single network group with its own per-group limit), and apply per-peer rate limiting on address advertisements.

### Proof of Concept
```rust
// Fill peer store with 16384 distinct Onion3 multiaddrs
// (each unique onion host:port pair)
// Then assert the next add_addr returns Err(EvictionFailed)
let mut store = PeerStore::default();
for i in 0u16..16384 {
    let onion_addr: Multiaddr = format!(
        "/onion3/vww6ybal4bd7szmgncyruucpgfkqahzddi37ktceo3ah7ngmcopnpyyd:{}/p2p/QmXXX",
        10000 + i
    ).parse().unwrap();
    // first 16383 succeed; 16384th triggers check_purge
    let _ = store.add_addr(onion_addr, Flags::SYNC);
}
// Now the store is at capacity with all Group::None entries
let new_addr: Multiaddr = "/onion3/differenthost:20000/p2p/QmYYY".parse().unwrap();
assert!(matches!(
    store.add_addr(new_addr, Flags::SYNC),
    Err(e) if e.to_string().contains("EvictionFailed")
));
```

### Citations

**File:** network/src/network_group.rs (L13-42)
```rust
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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```
