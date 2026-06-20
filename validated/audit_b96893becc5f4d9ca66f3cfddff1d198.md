Let me check the `AddrInfo::new` implementation and `is_connectable` to complete the analysis.

All the necessary code has been traced. Here is the complete analysis:

---

### Title
Peer Store Exhaustion via PeerId-less Multiaddr Injection in Discovery Protocol — (`network/src/protocols/discovery/mod.rs`, `network/src/peer_store/peer_store_impl.rs`)

### Summary

An unprivileged remote peer can inject `Multiaddr` values lacking a `/p2p/` (PeerId) component through the discovery `Nodes` message. These addresses pass `is_valid_addr()`, are stored in `AddrManager` as distinct entries (keyed on the full `Multiaddr`), are never consumed by connection logic (because `extract_peer_id` returns `None`), and are never evicted by `check_purge()` (because they are always `is_connectable`). With approximately 6 connections, an attacker can fill the store to `ADDR_COUNT_LIMIT = 16384`, after which `check_purge()` returns `Err(EvictionFailed)` and no new legitimate peer addresses can be added.

---

### Finding Description

**Step 1 — `is_valid_addr()` does not require a PeerId** [1](#0-0) 

The check only calls `is_reachable(socket_addr.ip())`. A bare `/ip4/1.2.3.4/tcp/8115` (no `/p2p/` component) passes if the IP is globally routable.

**Step 2 — `add_new_addrs()` passes the address directly to `add_addr()`** [2](#0-1) 

No PeerId check is performed before calling `peer_store.add_addr()`.

**Step 3 — `PeerStore::add_addr()` has no PeerId guard** [3](#0-2) 

The only checks are ban-list membership and `check_purge()`. A PeerId-less address is accepted.

**Step 4 — `AddrInfo::new` calls `base_addr()`, which strips only `Ws/Wss/Memory/Tls`, not `/p2p/`** [4](#0-3) [5](#0-4) 

The stored key is the full `Multiaddr` including any PeerId. `/ip4/1.2.3.4/tcp/8115` and `/ip4/1.2.3.4/tcp/8115/p2p/QmXxx` are two distinct keys. Addresses with different PeerIds for the same IP:port are also distinct keys.

**Step 5 — `AddrManager::add()` deduplicates by exact `Multiaddr`** [6](#0-5) 

Each unique `Multiaddr` (including PeerId-less ones) occupies a separate slot. An attacker sending 1000 items × 3 addresses per `Nodes` message injects up to 3000 entries per connection.

**Step 6 — Injected addresses are never consumed by connection logic** [7](#0-6) 

Both `fetch_addrs_to_feeler` and `fetch_addrs_to_attempt` use `extract_peer_id(&peer_addr.addr).map(...).unwrap_or_default()`. When `extract_peer_id` returns `None` (no `/p2p/` component), `unwrap_or_default()` yields `false`, so the address is silently skipped. It is never dialed, never marked tried, and never marked connected.

**Step 7 — Injected addresses are always `is_connectable`, surviving eviction** [8](#0-7) 

A freshly injected address has `last_connected_at_ms = 0`, `attempts_count = 0`, `last_tried_at_ms = 0`. The three `is_connectable` conditions all evaluate to `true` (never tried in last minute → skip; `attempts_count=0 < ADDR_MAX_RETRIES=3` → skip; `attempts_count=0 < ADDR_MAX_FAILURES=10` → skip). Result: `is_connectable` returns `true` indefinitely.

**Step 8 — `check_purge()` fails when injected addresses span many /16 subnets** [9](#0-8) 

- Pass 1: removes entries where `!is_connectable`. Injected addresses survive (Step 7).
- Pass 2: groups by network segment; evicts 2 random peers from groups with `len > 4`. If the attacker uses one address per /16 subnet (e.g., `1.0.0.1`, `2.0.0.1`, …), every group has exactly 1 entry → no eviction candidate → `candidate_peers.is_empty()` → returns `Err(PeerStoreError::EvictionFailed)`.

`add_addr()` propagates this error, and the new legitimate address is rejected.

**Step 9 — Rate: ~6 connections fill the store**

`ADDR_COUNT_LIMIT = 16384`. Per non-announce `Nodes` message: `MAX_ADDR_TO_SEND = 1000` items × `MAX_ADDRS = 3` addresses = 3000 entries. `⌈16384 / 3000⌉ = 6` connections. [10](#0-9) [11](#0-10) 

---

### Impact Explanation

Once the store is saturated with PeerId-less addresses spread across many /16 subnets, `check_purge()` returns `Err(EvictionFailed)` on every subsequent `add_addr()` call. The victim node can no longer learn about new legitimate peers via discovery. Existing connections are unaffected, but the node's ability to bootstrap, reconnect after churn, or expand its peer set is permanently impaired until the node is restarted (which clears the in-memory store).

---

### Likelihood Explanation

The attack requires only 6 TCP connections to the victim's P2P port and 6 well-formed `Nodes` messages. No authentication, no PoW, no privileged access. The attacker can use any 16384 globally routable IPs (e.g., from different /16 subnets) as address payloads. The `received_nodes` flag prevents sending a second non-announce `Nodes` per session, but the attacker simply reconnects. This is straightforward to automate.

---

### Recommendation

1. **Require PeerId in `add_addr()`**: Reject any `Multiaddr` for which `extract_peer_id()` returns `None` before inserting into `AddrManager`.
2. **Enforce PeerId check in `is_valid_addr()`**: Return `false` for addresses lacking a `/p2p/` component.
3. **Per-IP slot limit**: Cap the number of distinct PeerIds stored per IP address to prevent same-IP flooding with different PeerIds.
4. **Per-session injection rate limit**: Track how many addresses a single session has contributed and disconnect after a threshold.

---

### Proof of Concept

```rust
// Pseudocode: attacker sends 6 connections, each with 3000 PeerId-less addresses
for _ in 0..6 {
    connect_to_victim_p2p_port();
    let items: Vec<Node> = (0..1000).map(|i| Node {
        // Each address uses a distinct /16 subnet to defeat group eviction
        addresses: vec![
            format!("/ip4/{}.{}.0.1/tcp/8115", i / 256, i % 256)
                .parse::<Multiaddr>().unwrap(),  // no /p2p/ component
            // ... up to 3 per item
        ],
        flags: Flags::COMPATIBILITY,
    }).collect();
    send_nodes_message(Nodes { announce: false, items });
    // After this message, received_nodes=true; disconnect and reconnect
}
// Now peer_store.add_addr() returns Err(EvictionFailed) for any new legitimate peer
```

A unit test calling `peer_store.add_addr("/ip4/1.2.3.4/tcp/8115".parse().unwrap(), Flags::COMPATIBILITY)` would succeed today, confirming the missing PeerId guard.

### Citations

**File:** network/src/protocols/discovery/mod.rs (L32-34)
```rust
const MAX_ADDR_TO_SEND: usize = 1000;
// The maximum number addresses in one Nodes item
const MAX_ADDRS: usize = 3;
```

**File:** network/src/protocols/discovery/mod.rs (L332-341)
```rust
    fn is_valid_addr(&self, addr: &Multiaddr) -> bool {
        if !self.discovery_local_address {
            match multiaddr_to_socketaddr(addr) {
                Some(socket_addr) => is_reachable(socket_addr.ip()),
                None => true,
            }
        } else {
            true
        }
    }
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

**File:** network/src/peer_store/peer_store_impl.rs (L230-239)
```rust
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

**File:** network/src/peer_store/types.rs (L65-76)
```rust
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

**File:** network/src/peer_store/mod.rs (L92-104)
```rust
pub(crate) fn base_addr(addr: &Multiaddr) -> Multiaddr {
    addr.iter()
        .filter_map(|p| {
            if matches!(
                p,
                Protocol::Ws | Protocol::Wss | Protocol::Memory(_) | Protocol::Tls(_)
            ) {
                None
            } else {
                Some(p)
            }
        })
        .collect()
```

**File:** network/src/peer_store/addr_manager.rs (L22-42)
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

        let id = self.next_id;
        self.addr_to_id.insert(addr_info.addr.clone(), id);
        addr_info.random_id_pos = self.random_ids.len();
        self.id_to_info.insert(id, addr_info);
        self.random_ids.push(id);
        self.next_id += 1;
    }
```
