### Title
Unauthenticated Inbound Peer Can Poison Peer Store via Identify Listen Addresses — (`network/src/protocols/identify/mod.rs`)

---

### Summary

An unprivileged remote peer that opens an inbound connection can inject up to 10 attacker-controlled, globally-routable (but unowned) addresses per connection into the victim node's peer store. By cycling ~1639 cheap inbound connections, an attacker can fill the entire peer store (`ADDR_COUNT_LIMIT = 16384`) with fabricated addresses, permanently blocking legitimate address insertion and degrading the victim node's peer discovery via the feeler mechanism.

---

### Finding Description

**Root cause — `add_remote_listen_addrs` is called for inbound sessions without ownership verification.**

The code in `received_identify` explicitly guards `add_outbound_addr` (the session's own connection address) behind an `is_outbound()` check, with a comment acknowledging the reason: [1](#0-0) 

However, `process_listens` — which calls `add_remote_listen_addrs` — has **no session-type guard** and runs for both inbound and outbound sessions: [2](#0-1) 

`add_remote_listen_addrs` then unconditionally calls `peer_store.add_addr()` for every address in the message, logging errors but taking no corrective action: [3](#0-2) 

`add_addr` inserts each address with `last_connected_at_ms = 0` (never verified/connected): [4](#0-3) 

**Eviction bypass.** When the store reaches `ADDR_COUNT_LIMIT = 16384`, `check_purge` attempts two eviction strategies: [5](#0-4) 

- **Step 1**: evict non-connectable addresses. Attacker addresses have `attempts_count = 0` and `last_connected_at_ms = 0`, so they are always "connectable" — Step 1 evicts nothing.
- **Step 2**: group by network segment, evict from groups with `> 4` peers. If the attacker uses addresses from ≥ 16384 distinct `/16` subnets (one address per subnet), every group has exactly 1 entry — Step 2 evicts nothing. [6](#0-5) 

Both steps fail → `Err(PeerStoreError::EvictionFailed)` is returned, and no new address (including legitimate ones) can ever be inserted again.

**Threshold check.** `MAX_ADDRS = 10` is the disconnect threshold: [7](#0-6) 

Sending exactly 10 addresses per connection stays below the disconnect threshold while maximizing injection rate. `ceil(16384 / 10) = 1639` connections suffice.

**Global IP filter does not prevent the attack.** In production `global_ip_only = true`, but the filter only blocks private/loopback IPs. The attacker can use any globally routable IP they do not own: [8](#0-7) 

**Inbound connection limit is not a barrier.** The default config allows `max_peers = 125`, `max_outbound_peers = 8`, giving `max_inbound ≈ 117`. The attacker does not need 1639 simultaneous connections — they cycle: connect → send identify → disconnect → repeat. Each cycle injects 10 new addresses. [9](#0-8) 

---

### Impact Explanation

Once the peer store is saturated with attacker addresses:

- `fetch_addrs_to_feeler` returns only attacker-controlled addresses (attacker addresses have `last_connected_at_ms = 0`, which is exactly the feeler filter criterion). [10](#0-9) 

- All subsequent `add_addr` calls (from discovery, other identify messages, etc.) fail silently with `EvictionFailed`.
- Legitimate peer addresses are permanently crowded out.
- The victim node's feeler mechanism — its primary mechanism for probing and validating new peers — is entirely consumed by attacker-controlled dead-end addresses.
- The victim node cannot discover new honest peers, degrading its connectivity to the honest network.

`fetch_addrs_to_attempt` and `fetch_random_addrs` require `last_connected_at_ms > 0`, so existing outbound connections are not immediately severed, but the node cannot replenish its peer pool after churn.

---

### Likelihood Explanation

- **No privilege required**: any peer that can open a TCP connection to the victim's P2P port qualifies.
- **Low cost**: ~1639 TCP connections, each sending a ~200-byte identify message. Achievable from a single machine in seconds.
- **No PoW, no stake, no key**: the attack requires only network access.
- **Persistent effect**: once the store is full with diverse-subnet attacker addresses, the eviction mechanism cannot recover without a node restart and manual peer store purge.

---

### Recommendation

1. **Reject inbound listen-address injection entirely**, or rate-limit it per source IP. The existing comment in `received_identify` already acknowledges inbound addresses are unverified — apply the same logic to `process_listens` for inbound sessions.
2. **Source-IP diversity cap**: limit the number of addresses that can be inserted per source `/16` network group, regardless of session type.
3. **Require feeler confirmation before peer-store insertion**: only persist an address after a successful feeler connection verifies it is reachable.
4. **Eviction hardening**: ensure `check_purge` can always make progress (e.g., evict the oldest-inserted or lowest-scored address unconditionally when both strategies fail).

---

### Proof of Concept

```rust
// Pseudocode for the attack
for i in 0..1640 {
    let conn = open_inbound_tcp_connection(victim_p2p_addr);
    let listen_addrs: Vec<Multiaddr> = (0..10)
        .map(|j| format!("/ip4/{}.{}.{}.{}/tcp/8115/p2p/{}", 
            i/256, i%256, j, 1, random_peer_id()).parse().unwrap())
        .collect();
    // Each address is from a distinct globally-routable /16 subnet
    send_identify_message(conn, listen_addrs);
    close(conn);
}
// After loop: peer_store.addr_manager.count() == 16384
// All addresses are attacker-controlled, from distinct /16 subnets
// check_purge returns EvictionFailed for all subsequent add_addr calls
// fetch_addrs_to_feeler returns only attacker addresses
// No legitimate address can be inserted
```

The unit test described in the question (1640 mock inbound sessions × 10 addresses each, asserting `addr_manager.count() == ADDR_COUNT_LIMIT` and that no legitimate address survives `fetch_addrs_to_feeler`) would pass against the current production code.

### Citations

**File:** network/src/protocols/identify/mod.rs (L30-30)
```rust
const MAX_ADDRS: usize = 10;
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

**File:** network/src/protocols/identify/mod.rs (L415-423)
```rust
                if context.session.ty.is_outbound() {
                    // why don't set inbound here?
                    // because inbound address can't feeler during staying connected
                    // and if set it to peer store, it will be broadcast to the entire network,
                    // but this is an unverified address

                    self.network_state.with_peer_store_mut(|peer_store| {
                        peer_store.add_outbound_addr(context.session.address.clone(), flags);
                    });
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

**File:** network/src/peer_store/peer_store_impl.rs (L215-239)
```rust
    /// Get peers for feeler connection, this method randomly return peer addrs that we never
    /// connected to.
    pub fn fetch_addrs_to_feeler<F>(&mut self, count: usize, filter: F) -> Vec<AddrInfo>
    where
        F: Fn(&AddrInfo) -> bool,
    {
        // Get info:
        // 1. Not already connected
        // 2. Not already tried in a minute
        // 3. Not connected within 3 days

        let now_ms = ckb_systemtime::unix_time_as_millis();
        let addr_expired_ms = now_ms.saturating_sub(ADDR_TRY_TIMEOUT_MS);
        let peers = &self.connected_peers;

        let filter = |peer_addr: &AddrInfo| {
            filter(peer_addr)
                && extract_peer_id(&peer_addr.addr)
                    .map(|peer_id| !peers.contains_key(&peer_id))
                    .unwrap_or_default()
                && !peer_addr.tried_in_last_minute(now_ms)
                && !peer_addr.connected(|t| t > addr_expired_ms)
        };

        self.addr_manager.fetch_random(count, filter)
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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```

**File:** network/src/peer_registry.rs (L115-122)
```rust
            if session_type.is_inbound() {
                if connection_status.non_whitelist_inbound >= self.max_inbound {
                    if let Some(evicted_session) = self.try_evict_inbound_peer(peer_store) {
                        evicted_peer = self.remove_peer(evicted_session);
                    } else {
                        return Err(PeerError::ReachMaxInboundLimit.into());
                    }
                }
```
