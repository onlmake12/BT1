### Title
Peer Store Permanent Saturation via `check_purge` Eviction Dead Zone — (`network/src/peer_store/peer_store_impl.rs`)

---

### Summary

`check_purge` contains a logical dead zone: when all network groups in the peer store hold exactly ≤4 entries and all entries are connectable, neither eviction phase removes anything, and the function returns `PeerStoreError::EvictionFailed`. An unprivileged attacker can deliberately engineer this state via short-lived inbound P2P connections, each contributing crafted listen addresses, permanently blocking `add_addr` for all honest peers.

---

### Finding Description

**`check_purge` eviction logic** has two phases:

**Phase 1** — remove non-connectable entries: [1](#0-0) 

**Phase 2** — network-group eviction (only entered when Phase 1 removes nothing): [2](#0-1) 

The critical guard in Phase 2 is `if addrs.len() > 4` at line 378. Groups with exactly 4 entries are silently skipped. If every group has ≤4 entries and Phase 1 found nothing, `candidate_peers` is empty and the function returns `EvictionFailed`. [3](#0-2) 

**Network group granularity** is the first two octets of IPv4 (a /16, not /24 as the question states — the question's subnet description is slightly off, but the attack is identical): [4](#0-3) 

**Fresh entries are always connectable.** `add_addr` creates every entry with `last_connected_at_ms=0` and `attempts_count=0`: [5](#0-4) 

`is_connectable` returns `true` for these values because none of the three rejection branches fire (`attempts_count=0` is below both `ADDR_MAX_RETRIES=3` and `ADDR_MAX_FAILURES=10`): [6](#0-5) 

**Attack entry point.** Any inbound P2P connection triggers the Identify protocol. `process_listens` accepts up to `MAX_ADDRS=10` listen addresses and passes them to `add_remote_listen_addrs`: [7](#0-6) 

`add_remote_listen_addrs` calls `peer_store.add_addr` for each address without any per-connection rate limit: [8](#0-7) 

The only filter is `is_reachable` (globally routable IPs only), which the attacker satisfies trivially by using real public IP ranges in the listen address field — no ownership of those IPs is required. [9](#0-8) 

`ADDR_COUNT_LIMIT` is 16384: [10](#0-9) 

---

### Impact Explanation

Once the peer store holds 16384 entries spread across ≥4096 distinct /16 groups (≤4 per group), every subsequent call to `add_addr` hits `check_purge`, which returns `EvictionFailed`. The error is logged but swallowed in both callers (`add_remote_listen_addrs` and `add_new_addrs` in the Discovery protocol). The node can no longer record any new peer address. Peer discovery via both Identify and Discovery protocols is permanently blocked. Existing connections are unaffected, but the node cannot replenish its peer pool after churn, leading to progressive isolation.

---

### Likelihood Explanation

The attack requires ~1638 short-lived inbound TCP connections (16384 addresses ÷ 10 per connection). The default `max_peers=125` limits simultaneous connections, but the attacker does not need to hold connections open — connect, send Identify, disconnect, repeat. Addresses survive disconnection. No special privileges, no PoW, no key material. A single machine with one IP can execute this by cycling connections. The injected listen addresses need only be globally routable (e.g., `1.x.y.z`, `2.x.y.z`, etc.) — the node never verifies reachability before storing them.

---

### Recommendation

1. **Remove the `> 4` hard floor** in Phase 2, or replace it with a proportional eviction that always produces at least one candidate (e.g., evict from the largest group regardless of its size).
2. **Unconditionally evict** the oldest/least-recently-tried entry when no other candidate exists, rather than returning `EvictionFailed`.
3. **Rate-limit per-session address insertions** in `add_remote_listen_addrs` (e.g., cap total addresses accepted from a single session across its lifetime).
4. **Prefer evicting entries with `last_connected_at_ms=0`** (never successfully connected) before entries with a real connection history.

---

### Proof of Concept

```rust
// Fill 4096 distinct /16 groups with exactly 4 entries each = 16384 total.
// All entries: last_connected_at_ms=0, attempts_count=0 → is_connectable=true.
let mut peer_store = PeerStore::default();
let mut count = 0u32;
'outer: for a in 1u8..=255 {
    for b in 0u8..=255 {
        for port in 1u16..=4 {
            let addr: Multiaddr = format!(
                "/ip4/{}.{}.0.1/tcp/{}/p2p/{}",
                a, b, port, PeerId::random().to_base58()
            ).parse().unwrap();
            peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
            count += 1;
            if count == 16384 { break 'outer; }
        }
    }
}
// Peer store is now at ADDR_COUNT_LIMIT with 4096 groups × 4 entries each.
// All entries connectable. Phase 1 finds nothing. Phase 2: 4 > 4 is false for all groups.
let new_addr: Multiaddr = "/ip4/8.8.8.8/tcp/42/p2p/<new_peer_id>"
    .parse().unwrap();
let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
assert!(matches!(result, Err(e) if e.to_string().contains("EvictionFailed")));
```

### Citations

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

**File:** network/src/network_group.rs (L26-29)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
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

**File:** network/src/protocols/identify/mod.rs (L472-495)
```rust
    fn add_remote_listen_addrs(&mut self, session: &SessionContext, addrs: Vec<Multiaddr>) {
        trace!(
            "IdentifyProtocol add remote listening addresses, session: {:?}, addresses : {:?}",
            session, addrs,
        );
        let flags = self.network_state.with_peer_registry_mut(|reg| {
            if let Some(peer) = reg.get_peer_mut(session.id) {
                peer.listened_addrs = addrs.clone();
                peer.identify_info
                    .as_ref()
                    .map(|a| a.flags)
                    .unwrap_or(Flags::COMPATIBILITY)
            } else {
                Flags::COMPATIBILITY
            }
        });
        self.network_state.with_peer_store_mut(|peer_store| {
            for addr in addrs {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    error!("IdentifyProtocol failed to add address to peer store, address: {}, error: {:?}", addr, err);
                }
            }
        })
    }
```

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```
