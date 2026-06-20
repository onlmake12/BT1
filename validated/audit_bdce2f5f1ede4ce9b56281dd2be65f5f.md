### Title
Peer Store Address Exhaustion via Identify Protocol Listen Address Injection — (`network/src/peer_store/peer_store_impl.rs`, `network/src/protocols/identify/mod.rs`)

---

### Summary

An unprivileged remote attacker can exhaust the CKB peer store (`ADDR_COUNT_LIMIT = 16384`) by cycling through inbound connections and advertising 10 fabricated listen addresses per connection via the Identify protocol. Because `check_purge` only evicts addresses from network groups with **more than 4 peers**, and the attacker can spread all injected addresses across distinct `/16` subnets (one address per group), the eviction logic finds nothing to remove and returns `EvictionFailed`. After ~1639 sequential connections, the peer store is permanently full and all subsequent `add_addr` calls for legitimate peers fail silently.

---

### Finding Description

**Entry point — `IdentifyCallback::add_remote_listen_addrs`**

When any peer (inbound or outbound) sends an `IdentifyMessage`, `process_listens` is called unconditionally for all session types: [1](#0-0) 

If the list is ≤ `MAX_ADDRS = 10`, it passes through and `add_remote_listen_addrs` is called: [2](#0-1) 

There is no session-type guard here — inbound connections can inject addresses just as freely as outbound ones.

**`PeerStore::add_addr` — no per-peer or per-source limit** [3](#0-2) 

Every call goes straight to `check_purge()` then unconditionally inserts. There is no per-source-peer quota, no per-IP-range quota, and no rate limit.

**`check_purge` — two-stage eviction that can be defeated** [4](#0-3) 

Stage 1 evicts addresses where `is_connectable` returns `false`. A freshly injected address has `last_connected_at_ms = 0` and `attempts_count = 0`, so: [5](#0-4) 

`attempts_count (0) < ADDR_MAX_RETRIES (3)` → `is_connectable` returns `true` → **not evicted in stage 1**.

Stage 2 groups addresses by network group and evicts 2 random peers from groups with **more than 4 members**, but only from the top half of groups by size: [6](#0-5) 

**Network group granularity** is the first two octets of IPv4 (`/16`): [7](#0-6) 

IPv4 has 65,536 possible `/16` groups. If the attacker places exactly one address per `/16` group, every group has size 1, which is ≤ 4, so the `if addrs.len() > 4` branch is never taken, `candidate_peers` is empty, and: [8](#0-7) 

`EvictionFailed` is returned, `add_addr` propagates the error, and the peer store is permanently blocked.

---

### Impact Explanation

- The victim node's peer store is filled with 16,384 attacker-controlled, never-verified addresses.
- All subsequent `add_addr` calls (from Discovery, Identify, or outbound connection recording) return `EvictionFailed` and are silently dropped.
- The node cannot learn new honest peer addresses, degrading outbound connection quality and network topology discovery.
- The attack persists across reconnections because the injected addresses remain in the store (they are "connectable" by the store's own logic and are never naturally evicted until `attempts_count ≥ 3` after actual dial attempts, which the node will waste time on).

---

### Likelihood Explanation

- **Cost is extremely low**: ~1,639 sequential TCP connections, each sending one Identify message with 10 addresses from distinct `/16` subnets, then disconnecting. No simultaneous connection limit is relevant.
- **Addresses need not be real**: the only filter is `is_reachable` (global IP check). The attacker can advertise any globally routable IPs they do not own.
- **No PoW, no stake, no privileged role** is required — any node that can open a TCP connection to the victim can execute this.
- **Inbound connections are accepted by default**, so the attacker does not need the victim to dial them.

---

### Recommendation

1. **Per-source-peer address quota**: in `add_remote_listen_addrs`, track how many addresses have been contributed by each peer (keyed by peer ID or source IP) and reject contributions beyond a small limit (e.g., 10 total per peer ID, not per message).
2. **Lower the eviction threshold**: the `> 4` threshold in `check_purge` is too permissive. Evict from any group with ≥ 2 members, or use a probabilistic eviction that always makes room.
3. **Require prior successful connection before storing**: addresses received via Identify from inbound peers should not be stored in the peer store at all (the code comment at line 417–419 acknowledges this concern for `add_outbound_addr` but the `add_remote_listen_addrs` path has no such guard).
4. **Rate-limit address insertion per source IP**: cap the total number of addresses that can be injected from any single `/16` source network within a time window.

---

### Proof of Concept

```rust
// Pseudocode unit test
let mut peer_store = PeerStore::default();
let flags = Flags::COMPATIBILITY;

// Attacker cycles 1639 inbound connections,
// each advertising 10 addresses from distinct /16 subnets.
// Total: 16384 addresses, one per /16 group.
for i in 0u32..16384 {
    let a = (i >> 8) as u8;
    let b = (i & 0xff) as u8;
    // Each address is in a unique /16: a.b.0.1
    let addr: Multiaddr = format!("/ip4/{}.{}.0.1/tcp/8115/p2p/Qm...", a, b)
        .parse().unwrap();
    // This succeeds for the first 16383 insertions
    peer_store.add_addr(addr, flags).unwrap();
}

// Now the store is at ADDR_COUNT_LIMIT.
// check_purge: stage 1 finds nothing (all attempts_count=0 < ADDR_MAX_RETRIES=3).
// check_purge: stage 2 finds no group with >4 members (all groups have 1).
// Result: EvictionFailed.
let honest_addr: Multiaddr = "/ip4/200.200.200.1/tcp/8115/p2p/Qm...".parse().unwrap();
assert!(matches!(
    peer_store.add_addr(honest_addr, flags),
    Err(e) if e is PeerStoreError::EvictionFailed
));
```

### Citations

**File:** network/src/protocols/identify/mod.rs (L134-148)
```rust
        if listens.len() > MAX_ADDRS {
            self.callback
                .misbehave(&info.session, Misbehavior::TooManyAddresses(listens.len()))
        } else {
            let global_ip_only = self.global_ip_only;
            let reachable_addrs = listens
                .into_iter()
                .filter(|addr| match multiaddr_to_socketaddr(addr) {
                    Some(socket_addr) => !global_ip_only || is_reachable(socket_addr.ip()),
                    None => true,
                })
                .collect::<Vec<_>>();
            self.callback
                .add_remote_listen_addrs(session, reachable_addrs);
            MisbehaveResult::Continue
```

**File:** network/src/protocols/identify/mod.rs (L488-494)
```rust
        self.network_state.with_peer_store_mut(|peer_store| {
            for addr in addrs {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    error!("IdentifyProtocol failed to add address to peer store, address: {}, error: {:?}", addr, err);
                }
            }
        })
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
