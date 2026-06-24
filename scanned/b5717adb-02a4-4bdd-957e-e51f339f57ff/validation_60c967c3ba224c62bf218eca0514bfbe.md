Audit Report

## Title
Unbounded Peer Store Flooding via Discovery `Nodes` Messages Enables Feeler Slot Hijacking — (`network/src/protocols/discovery/mod.rs`, `network/src/peer_store/peer_store_impl.rs`)

## Summary

A single remote peer with one established Discovery session can flood the victim's peer store to its hard cap (`ADDR_COUNT_LIMIT = 16384`) by sending repeated announce `Nodes` messages containing unique, globally-routable addresses spread across many `/24` subnets. Because `add_new_addrs` performs no per-peer rate limiting, `check_purge` eviction is bypassable, and `fetch_addrs_to_feeler` selects randomly from the flooded pool, all feeler connection slots are consumed by attacker-controlled TCP black-hole addresses, effectively starving the node of honest outbound peer discovery.

## Finding Description

**Root cause 1 — No per-peer rate limiting in `add_new_addrs`**

The `_session_id` parameter in `DiscoveryAddressManager::add_new_addrs` is explicitly discarded (underscore prefix). Every address in the incoming batch is unconditionally forwarded to `peer_store.add_addr()` with no per-session counter, quota, or cooldown. [1](#0-0) 

**Root cause 2 — Announce messages bypass the one-shot non-announce guard**

`verify_nodes_message` enforces `MAX_ADDR_TO_SEND = 1000` items for non-announce messages and `ANNOUNCE_THRESHOLD = 10` items for announce messages. The `received_nodes` flag blocks duplicate non-announce messages, but announce messages carry no such guard and can be sent an unlimited number of times per session. [2](#0-1) [3](#0-2) [4](#0-3) 

**Root cause 3 — `check_purge` eviction is bypassable**

`add_addr` calls `check_purge` before inserting. When the store reaches `ADDR_COUNT_LIMIT = 16384`:

- **Step 1** evicts addresses where `is_connectable()` returns `false`. Fresh attacker addresses have `attempts_count = 0` and `last_connected_at_ms = 0`; `is_connectable` only returns `false` when `last_connected_at_ms == 0 && attempts_count >= ADDR_MAX_RETRIES (3)`. With `attempts_count = 0`, all attacker addresses are considered connectable and survive this step.
- **Step 2** groups by `/24` network segment and evicts 2 from groups with **more than 4** peers. An attacker using ≤4 unique addresses per `/24` subnet bypasses this entirely.
- **Fallback** returns `Err(PeerStoreError::EvictionFailed)`, which `add_new_addrs` silently ignores (logs at `debug` level only). [5](#0-4) [6](#0-5) [7](#0-6) 

**Root cause 4 — `fetch_addrs_to_feeler` selects randomly from the flooded pool**

`fetch_addrs_to_feeler` calls `addr_manager.fetch_random(FEELER_CONNECTION_COUNT=10, filter)`. The filter accepts any address that is not currently connected, not tried in the last minute, and not connected within 3 days (`ADDR_TRY_TIMEOUT_MS`). Attacker addresses (`last_connected_at_ms = 0`, `attempts_count = 0`) satisfy all three conditions. `fetch_random` uses a Fisher-Yates shuffle and deduplicates by IP — but since the attacker uses unique IPs, deduplication provides no protection. [8](#0-7) [9](#0-8) [10](#0-9) 

**Exploit flow:**

1. Attacker establishes one Discovery session with the victim.
2. Sends 1 non-announce `Nodes` message: 1000 items × 3 addresses = 3,000 addresses inserted.
3. Sends ~450 announce `Nodes` messages (10 items × 3 addresses each): 13,500 more addresses inserted.
4. All attacker IPs are spread across unique `/24` subnets (≤4 per subnet), defeating both eviction steps.
5. Peer store reaches `ADDR_COUNT_LIMIT = 16384`; `check_purge` returns `EvictionFailed`; legitimate addresses cannot enter.
6. `dial_feeler` fires every interval: `fetch_addrs_to_feeler` returns 10 random entries from 16,384 attacker addresses. P(selecting any of ~50 legitimate addresses) ≈ 0.3% per slot.
7. Attacker sustains the flood by continuing to send announce messages, replenishing any addresses that accumulate `attempts_count >= 3` with fresh ones. [11](#0-10) 

## Impact Explanation

The victim node's peer store is permanently saturated with attacker-supplied, unreachable addresses. All `FEELER_CONNECTION_COUNT = 10` feeler slots per interval are consumed by TCP black-hole connections. Legitimate peer addresses cannot enter the store, and the random feeler selection never reaches honest peers. If existing outbound connections are lost (network churn, restarts), the node cannot recover peer discovery and becomes progressively isolated. If applied at scale across many nodes simultaneously, this degrades the broader CKB peer-discovery mesh.

This maps to: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs (10001–15000 points).**

## Likelihood Explanation

The precondition is a single established Discovery session — achievable by any peer that connects to the victim. The attacker needs only globally-routable IPs spread across many `/24` subnets (e.g., using allocated or spoofed address space). No special privilege, no PoW, and no key material is required. The attack is repeatable across node restarts if the peer store is persisted, and is sustainable as long as the Discovery session is maintained.

## Recommendation

1. **Per-peer address ingestion rate limit**: Track the number of addresses accepted per `session_id` within a rolling time window inside `add_new_addrs`. Reject excess addresses beyond a per-session quota (e.g., 500 addresses per 10-minute window).
2. **Per-network-group insertion cap**: Before calling `addr_manager.add()`, check whether the source `/24` already has ≥ N entries and reject the new address if so, mirroring Bitcoin Core's `addrman` bucketing.
3. **Eviction preference for unverified addresses**: In `check_purge`, prioritize eviction of addresses with `last_connected_at_ms == 0` and `attempts_count == 0` (never tried, never connected) before applying the network-group strategy.
4. **Feeler selection bias toward verified addresses**: In `fetch_addrs_to_feeler`, prefer addresses with at least one prior successful connection before falling back to never-connected entries.

## Proof of Concept

```
1. Attacker connects to victim → Discovery session established.
2. Attacker sends 1 non-announce Nodes message:
     items: 1000 × { addresses: [unique_ip_1:8115, unique_ip_2:8115, unique_ip_3:8115] }
   → 3,000 addresses inserted (no rate check; _session_id ignored).
3. Attacker sends ~450 announce Nodes messages (10 items × 3 addrs each):
   → 13,500 more addresses inserted.
   → peer store reaches ADDR_COUNT_LIMIT = 16,384.
4. All attacker IPs spread across unique /24 subnets (≤4 per subnet):
   → check_purge step 1: all connectable (attempts_count=0 < ADDR_MAX_RETRIES=3) → no eviction.
   → check_purge step 2: no group > 4 → no eviction.
   → Err(EvictionFailed) silently ignored; legitimate addresses cannot enter.
5. OutboundPeerService fires dial_feeler every interval:
   → fetch_addrs_to_feeler returns 10 random entries from 16,384 attacker addresses.
   → P(selecting any of ~50 legitimate addresses) ≈ 50/16384 ≈ 0.3% per slot.
   → All 10 feeler connections go to TCP black holes; honest peers never dialed.
6. Attacker sustains attack by sending additional announce messages to replenish
   any addresses that accumulate attempts_count >= 3.
7. Assert: peer_store address count == 16,384, all attacker-controlled;
           feeler dials never reach honest peers; node peer discovery is hijacked.
```

### Citations

**File:** network/src/protocols/discovery/mod.rs (L29-36)
```rust
const ANNOUNCE_CHECK_INTERVAL: Duration = Duration::from_secs(60);
const ANNOUNCE_THRESHOLD: usize = 10;
// The maximum number of new addresses to accumulate before announcing.
const MAX_ADDR_TO_SEND: usize = 1000;
// The maximum number addresses in one Nodes item
const MAX_ADDRS: usize = 3;
// Every 24 hours send announce nodes message
const ANNOUNCE_INTERVAL: Duration = Duration::from_secs(3600 * 24);
```

**File:** network/src/protocols/discovery/mod.rs (L180-206)
```rust
                        if let Some(state) = self.sessions.get_mut(&session.id) {
                            if !nodes.announce && state.received_nodes {
                                warn!("Nodes (announce=false) message received");
                                if check(Misbehavior::DuplicateFirstNodes)
                                    && context.disconnect(session.id).await.is_err()
                                {
                                    debug!("Disconnect {:?} msg failed to send", session.id)
                                }
                            } else {
                                let addrs = nodes
                                    .items
                                    .into_iter()
                                    .flat_map(|node| {
                                        node.addresses.into_iter().map(move |a| (a, node.flags))
                                    })
                                    .collect::<Vec<_>>();

                                state.addr_known.extend(addrs.iter());
                                // Non-announce nodes can only receive once
                                // Due to the uncertainty of the other party’s state,
                                // the announce node may be sent out first, and it must be
                                // determined to be Non-announce before the state can be changed
                                if !nodes.announce {
                                    state.received_nodes = true;
                                }
                                self.addr_mgr.add_new_addrs(session.id, addrs);
                            }
```

**File:** network/src/protocols/discovery/mod.rs (L266-299)
```rust
fn verify_nodes_message(nodes: &Nodes) -> Option<Misbehavior> {
    let mut misbehavior = None;
    if nodes.announce {
        if nodes.items.len() > ANNOUNCE_THRESHOLD {
            warn!(
                "Number of nodes exceeds announce threshold {}",
                ANNOUNCE_THRESHOLD
            );
            misbehavior = Some(Misbehavior::TooManyItems {
                announce: nodes.announce,
                length: nodes.items.len(),
            });
        }
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

    if misbehavior.is_none() {
        for item in &nodes.items {
            if item.addresses.len() > MAX_ADDRS {
                misbehavior = Some(Misbehavior::TooManyAddresses(item.addresses.len()));
                break;
            }
        }
    }

    misbehavior
```

**File:** network/src/protocols/discovery/mod.rs (L347-363)
```rust
    fn add_new_addrs(&mut self, _session_id: SessionId, addrs: Vec<(Multiaddr, Flags)>) {
        if addrs.is_empty() {
            return;
        }

        for (addr, flags) in addrs.into_iter().filter(|addr| self.is_valid_addr(&addr.0)) {
            trace!("Add discovered address:{:?}", addr);
            self.network_state.with_peer_store_mut(|peer_store| {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    debug!(
                        "Failed to add discovered address to peer_store {:?} {:?}",
                        err, addr
                    );
                }
            });
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

**File:** network/src/peer_store/peer_store_impl.rs (L217-240)
```rust
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

**File:** network/src/peer_store/addr_manager.rs (L44-97)
```rust
    /// Randomly return addrs that worth to try or connect.
    pub fn fetch_random<F>(&mut self, count: usize, filter: F) -> Vec<AddrInfo>
    where
        F: Fn(&AddrInfo) -> bool,
    {
        let mut duplicate_ips = HashSet::new();
        let mut addr_infos = Vec::with_capacity(count);
        let mut rng = rand::thread_rng();
        let now_ms = ckb_systemtime::unix_time_as_millis();
        for i in 0..self.random_ids.len() {
            // reuse the for loop to shuffle random ids
            // https://en.wikipedia.org/wiki/Fisher%E2%80%93Yates_shuffle
            let j = rng.gen_range(i..self.random_ids.len());
            self.swap_random_id(j, i);
            let addr_info: AddrInfo = self.id_to_info[&self.random_ids[i]].to_owned();
            match multiaddr_to_socketaddr(&addr_info.addr) {
                Some(socket_addr) => {
                    let ip = socket_addr.ip();
                    let is_unique_ip = !duplicate_ips.contains(&ip);
                    // A trick to make our tests work
                    // TODO remove this after fix the network tests.
                    let is_test_ip = ip.is_unspecified() || ip.is_loopback();
                    if (is_test_ip || is_unique_ip)
                        && addr_info.is_connectable(now_ms)
                        && filter(&addr_info)
                    {
                        duplicate_ips.insert(ip);
                        addr_infos.push(addr_info);
                    }
                }
                None => {
                    if filter(&addr_info) {
                        if addr_info.is_connectable(now_ms)
                            || addr_info
                                .addr
                                .iter()
                                .any(|p| matches!(p, Protocol::Onion3(_)))
                        {
                            addr_infos.push(addr_info);
                        } else {
                            debug!(
                                "addr {:?} is not connectable and not an onion address",
                                addr_info.addr
                            );
                        }
                    }
                }
            }
            if addr_infos.len() == count {
                break;
            }
        }
        addr_infos
    }
```

**File:** network/src/services/outbound_peer.rs (L22-22)
```rust
const FEELER_CONNECTION_COUNT: usize = 10;
```

**File:** network/src/services/outbound_peer.rs (L56-96)
```rust
    fn dial_feeler(&mut self) {
        let now_ms = unix_time_as_millis();
        let filter = |peer_addr: &AddrInfo| match self.transport_type {
            TransportType::Tcp => true,
            TransportType::Ws => peer_addr
                .addr
                .iter()
                .any(|p| matches!(p, Protocol::Dns4(_) | Protocol::Dns6(_) | Protocol::Tcp(_))),
            TransportType::Wss => peer_addr
                .addr
                .iter()
                .any(|p| matches!(p, Protocol::Dns4(_) | Protocol::Dns6(_))),
        };
        let attempt_peers = self.network_state.with_peer_store_mut(|peer_store| {
            let paddrs = peer_store.fetch_addrs_to_feeler(FEELER_CONNECTION_COUNT, filter);
            for paddr in paddrs.iter() {
                // mark addr as tried
                if let Some(paddr) = peer_store.mut_addr_manager().get_mut(&paddr.addr) {
                    paddr.mark_tried(now_ms);
                }
            }
            paddrs
        });

        trace!(
            "feeler dial count={}, attempt_peers: {:?}",
            attempt_peers.len(),
            attempt_peers,
        );

        for mut addr in attempt_peers.into_iter().map(|info| info.addr) {
            self.network_state.dial_feeler(&self.p2p_control, {
                match &self.transport_type {
                    TransportType::Tcp => (),
                    TransportType::Ws => addr.push(Protocol::Ws),
                    TransportType::Wss => addr.push(Protocol::Wss),
                }
                addr
            });
        }
    }
```
