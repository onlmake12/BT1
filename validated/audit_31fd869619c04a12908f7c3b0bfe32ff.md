## Vulnerability Analysis

I traced the full code path carefully. The vulnerability is real.

---

### Title
Peer Store Permanently Blocked via Crafted Identify `listen_addrs` Flooding — (`network/src/peer_store/peer_store_impl.rs`)

---

### Summary

An unprivileged remote peer can fill the `PeerStore` to `ADDR_COUNT_LIMIT=16384` with attacker-controlled addresses that are all "connectable" and spread across many distinct `/16` network groups (≤ 4 per group). This defeats both eviction strategies in `check_purge`, causing it to return `PeerStoreError::EvictionFailed` permanently. All subsequent `add_addr` calls from honest peers silently fail, blocking peer discovery indefinitely.

---

### Finding Description

**Entry point — `process_listens` / `add_remote_listen_addrs`:**

`process_listens` accepts up to `MAX_ADDRS=10` listen addresses per identify message from any remote peer. It only checks that the count does not exceed 10 and that addresses are globally reachable IPs. It does not rate-limit, deduplicate across sessions, or restrict which IPs can be reported. [1](#0-0) 

These addresses are forwarded to `add_remote_listen_addrs`, which calls `peer_store.add_addr` for each one: [2](#0-1) 

**`add_addr` calls `check_purge` before inserting:** [3](#0-2) 

**`check_purge` has two eviction strategies:**

*Strategy 1* — evict addresses where `is_connectable(now_ms) == false`: [4](#0-3) 

*Strategy 2* — if strategy 1 finds nothing, group by `/16` subnet and evict 2 random peers from groups with `> 4` members: [5](#0-4) 

**`is_connectable` returns `true` for all freshly-added addresses:**

`add_addr` inserts with `last_connected_at_ms=0` and `attempts_count=0`. The `is_connectable` check only returns `false` when `attempts_count >= ADDR_MAX_RETRIES (3)` (never connected) or `attempts_count >= ADDR_MAX_FAILURES (10)` (connected but stale). Fresh addresses with `attempts_count=0` always return `true`. [6](#0-5) 

**Defeating strategy 2:**

The network group is keyed on the first two octets of the IPv4 address (`[bits[0], bits[1]]`): [7](#0-6) 

If the attacker spreads 16384 addresses across ≥ 4097 distinct `/16` subnets (≤ 4 addresses per subnet), no group ever has `> 4` members. The inner `candidate_peers` list remains empty, and `EvictionFailed` is returned: [8](#0-7) 

**Error is silently swallowed:**

The caller in `add_remote_listen_addrs` only logs the error — it does not disconnect the session or take any corrective action: [9](#0-8) 

---

### Impact Explanation

Once the peer store is full and eviction is permanently blocked:
- All `add_addr` calls return `Err` and are silently dropped.
- The victim node can no longer learn about new peers via the identify protocol.
- Peer discovery is effectively disabled. The node cannot expand its peer set beyond currently connected peers.
- If existing connections drop (churn, restarts, bans), the node cannot replace them and becomes progressively more isolated.

---

### Likelihood Explanation

The attack requires making approximately ⌈16384 / 10⌉ = **1639 sequential TCP connections**, each sending 10 unique globally-reachable listen addresses from distinct `/16` subnets. The attacker:
- Does **not** need to own those IPs — they are just reported as listen addresses.
- Does **not** need simultaneous connections — sequential connections accumulate in the persistent peer store.
- Does **not** need any privileged role.
- Can use a single source IP (no Sybil requirement), since the peer store stores *reported listen addresses*, not the connection source.

The only friction is the connection rate and the need for globally-routable IP addresses in the payload. Both are trivially satisfied by any attacker with internet access.

---

### Recommendation

1. **Rate-limit address contributions per source IP or per session** — cap how many addresses a single peer can contribute to the peer store over a time window.
2. **Fix the eviction fallback** — when all addresses are connectable and no group has `> 4` members, fall back to evicting the oldest or lowest-scored addresses rather than returning `EvictionFailed`.
3. **Prefer evicting addresses that were never successfully connected** (`last_connected_at_ms == 0`) before addresses with a connection history.
4. **Bound the number of addresses accepted per identify session** at the peer store level, not just at the protocol level.

---

### Proof of Concept

```rust
// Fill peer store with 16384 addresses across 4097 distinct /16 subnets (≤ 4 per subnet)
// Each address: last_connected_at_ms=0, attempts_count=0 → is_connectable=true
let mut peer_store = PeerStore::default();
let mut count = 0u32;
'outer: for a in 1u8..=255 {
    for b in 0u8..=255 {
        for port in 1u16..=4 {
            if count >= 16384 { break 'outer; }
            let addr: Multiaddr = format!(
                "/ip4/{}.{}.1.1/tcp/{}/p2p/{}", a, b, port, PeerId::random().to_base58()
            ).parse().unwrap();
            peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
            count += 1;
        }
    }
}
assert_eq!(peer_store.addr_manager().count(), 16384);

// Now any new add_addr returns EvictionFailed
let honest_addr: Multiaddr = format!(
    "/ip4/8.8.8.1/tcp/9999/p2p/{}", PeerId::random().to_base58()
).parse().unwrap();
let result = peer_store.add_addr(honest_addr, Flags::COMPATIBILITY);
assert!(result.is_err()); // PeerStoreError::EvictionFailed
```

### Citations

**File:** network/src/protocols/identify/mod.rs (L123-150)
```rust
    fn process_listens(
        &mut self,
        context: &mut ProtocolContextMutRef,
        listens: Vec<Multiaddr>,
    ) -> MisbehaveResult {
        let session = context.session;
        let info = self
            .remote_infos
            .get_mut(&session.id)
            .expect("RemoteInfo must exists");

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
        }
    }
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

**File:** network/src/peer_store/peer_store_impl.rs (L340-355)
```rust
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
