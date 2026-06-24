Audit Report

## Title
Unauthenticated Inbound Peer Can Poison Peer Store via Identify Listen Addresses — (`network/src/protocols/identify/mod.rs`)

## Summary
`process_listens` in the Identify protocol accepts and stores up to 10 attacker-supplied listen addresses per inbound session without any session-type guard, despite the adjacent `add_outbound_addr` path being explicitly gated on `is_outbound()`. By cycling ~1639 cheap inbound connections, each injecting 10 globally-routable addresses from distinct `/16` subnets, an attacker can fill the entire peer store (`ADDR_COUNT_LIMIT = 16384`) with fabricated addresses. Both eviction strategies in `check_purge` then fail permanently, blocking all subsequent legitimate address insertion and causing the feeler mechanism to exclusively probe attacker-controlled dead-end addresses.

## Finding Description

**Root cause — missing session-type guard in `process_listens`.**

`received_identify` explicitly gates `add_outbound_addr` behind `is_outbound()` with a comment acknowledging that inbound addresses are unverified: [1](#0-0) 

However, `process_listens` — called unconditionally for every session — has no such guard: [2](#0-1) 

`add_remote_listen_addrs` then calls `peer_store.add_addr()` for every address, logging errors but taking no corrective action: [3](#0-2) 

`add_addr` inserts each address with `last_connected_at_ms = 0` (never verified): [4](#0-3) 

**Eviction bypass — both `check_purge` strategies fail.**

Step 1 evicts addresses where `is_connectable()` returns false. For freshly injected attacker addresses (`last_connected_at_ms = 0`, `attempts_count = 0`, `last_tried_at_ms = 0`), all three non-connectable conditions evaluate false: [5](#0-4) 

Step 2 groups by network segment and only evicts from groups with `> 4` peers. If the attacker uses one address per distinct `/16` subnet (trivially achievable across the global IPv4 space), every group has exactly 1 entry and the `> 4` threshold is never met: [6](#0-5) 

Both steps fail → `Err(PeerStoreError::EvictionFailed)` is returned, and no new address can ever be inserted.

**Feeler mechanism capture.** `fetch_addrs_to_feeler` selects addresses where `connected(|t| t > addr_expired_ms)` is false — exactly the condition satisfied by attacker addresses with `last_connected_at_ms = 0`: [7](#0-6) 

**Threshold and limit constants confirmed:** [8](#0-7) [9](#0-8) 

## Impact Explanation

Once the peer store is saturated with 16384 attacker addresses from distinct `/16` subnets:
- All subsequent `add_addr` calls (from discovery, other identify messages) return `EvictionFailed` silently.
- `fetch_addrs_to_feeler` returns exclusively attacker-controlled addresses, consuming the node's entire peer probing budget on dead-end connections.
- `fetch_addrs_to_attempt` and `fetch_random_addrs` require `last_connected_at_ms > 0`, so they return nothing from the poisoned entries — but the node cannot replenish its peer pool after natural churn.
- The victim node is progressively isolated from the honest network.

Applied at scale across many nodes simultaneously, this degrades the CKB network's peer discovery infrastructure at very low cost, matching **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation

- **No privilege required**: any peer that can open a TCP connection to the victim's P2P port qualifies.
- **Low cost**: ~1639 sequential TCP connections, each sending a small identify message. The inbound limit (`max_inbound ≈ 117`) is not a barrier because the attacker cycles connections (connect → send identify → disconnect → repeat); no simultaneous connections are needed.
- **Global IP filter is not a barrier**: `global_ip_only = true` only blocks private/loopback ranges; globally routable IPs the attacker does not own pass the filter freely.
- **Persistent and irreversible**: once the store is full with diverse-subnet attacker addresses, the eviction mechanism cannot recover without a node restart and manual peer store purge.

## Recommendation

1. **Reject inbound listen-address injection**: apply the same `is_outbound()` guard used for `add_outbound_addr` to `process_listens`, or skip `add_remote_listen_addrs` entirely for inbound sessions. The existing comment at line 416 already articulates the correct rationale.
2. **Source-IP diversity cap**: limit the number of addresses insertable per source `/16` network group regardless of session type.
3. **Feeler confirmation before persistence**: only persist an address in the peer store after a successful feeler connection verifies reachability.
4. **Unconditional eviction fallback**: ensure `check_purge` can always make progress (e.g., evict the oldest-inserted or lowest-scored address when both strategies fail) so the store never becomes permanently locked.

## Proof of Concept

```rust
// Pseudocode — each iteration is a new inbound TCP session
for i in 0u32..1640 {
    let conn = open_inbound_tcp_connection(victim_p2p_addr);
    // 10 addresses, each from a distinct globally-routable /16 subnet
    let listen_addrs: Vec<Multiaddr> = (0..10u32)
        .map(|j| {
            // i selects the /16, j selects the host — all globally routable
            format!("/ip4/{}.{}.1.1/tcp/8115/p2p/{}", 
                (i / 256) + 1, i % 256, random_peer_id())
                .parse().unwrap()
        })
        .collect();
    send_identify_message(conn, listen_addrs); // stays at or below MAX_ADDRS=10
    close(conn);
}
// Postconditions:
// peer_store.addr_manager.count() == ADDR_COUNT_LIMIT (16384)
// check_purge() returns Err(EvictionFailed) for all subsequent add_addr calls
// fetch_addrs_to_feeler() returns only attacker addresses (last_connected_at_ms == 0)
// No legitimate address can be inserted
```

A unit test constructing 1640 mock inbound `SessionContext` objects, calling `add_remote_listen_addrs` with 10 distinct globally-routable addresses each, and then asserting `addr_manager.count() == ADDR_COUNT_LIMIT` and that `add_addr` returns `Err(EvictionFailed)` would pass against the current production code.

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

**File:** network/src/peer_store/peer_store_impl.rs (L230-237)
```rust
        let filter = |peer_addr: &AddrInfo| {
            filter(peer_addr)
                && extract_peer_id(&peer_addr.addr)
                    .map(|peer_id| !peers.contains_key(&peer_id))
                    .unwrap_or_default()
                && !peer_addr.tried_in_last_minute(now_ms)
                && !peer_addr.connected(|t| t > addr_expired_ms)
        };
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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```
