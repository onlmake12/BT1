Based on my analysis of the code, I need to trace the full attack path carefully.

**Key code locations examined:**

1. `add_addr()` validation — `peer_store_impl.rs:71-80`
2. `fetch_addrs_to_feeler()` filter — `peer_store_impl.rs:230-237`
3. `fetch_addrs_to_attempt()` filter — `peer_store_impl.rs:201-209`
4. `AddrManager::fetch_random()` — `addr_manager.rs:44-97`
5. Discovery `add_new_addrs()` — `protocols/discovery/mod.rs:347-363`
6. `is_valid_addr()` — `protocols/discovery/mod.rs:332-341`
7. `is_connectable()` — `peer_store/types.rs:89-105`
8. `check_purge()` — `peer_store_impl.rs:327-404`

---

### Title
Peer Store Slot Exhaustion via Peer-ID-Less Addresses Injected Through Discovery Protocol — (`network/src/peer_store/peer_store_impl.rs`, `network/src/protocols/discovery/mod.rs`)

### Summary

The discovery protocol accepts and stores multiaddrs that lack a `/p2p/<peer_id>` component. Once stored, these addresses are permanently excluded from both feeler and attempt selection due to `extract_peer_id(...).unwrap_or_default()` evaluating to `false`. Because they are never dialed, their `attempts_count` never increments, so `is_connectable()` always returns `true`, making them immune to normal eviction. An attacker can fill all 16,384 peer store slots with such addresses using only a handful of discovery messages.

### Finding Description

**Step 1 — Attacker-controlled entry point.**

A connected peer sends a `DiscoveryMessage::Nodes` message. The handler in `DiscoveryProtocol::received()` calls `addr_mgr.add_new_addrs(session_id, addrs)` with the raw multiaddrs from the message. [1](#0-0) 

**Step 2 — No peer_id validation in `add_new_addrs` / `is_valid_addr`.**

`DiscoveryAddressManager::add_new_addrs()` filters only through `is_valid_addr()`, which checks only whether the IP is publicly reachable. There is no check for the presence of a `/p2p/` component. [2](#0-1) [3](#0-2) 

**Step 3 — `add_addr()` stores the address without peer_id validation.**

`PeerStore::add_addr()` only checks the ban list. It passes the address directly to `addr_manager.add()` with no requirement for a `/p2p/` component. [4](#0-3) 

**Step 4 — `unwrap_or_default()` permanently excludes peer-id-less addresses from selection.**

Both `fetch_addrs_to_feeler()` and `fetch_addrs_to_attempt()` use the same pattern:

```rust
extract_peer_id(&peer_addr.addr)
    .map(|peer_id| !peers.contains_key(&peer_id))
    .unwrap_or_default()
```

When `extract_peer_id` returns `None` (no `/p2p/` component), `.unwrap_or_default()` returns `false` (the default for `bool`), causing the address to be **unconditionally excluded** from both feeler and attempt selection. [5](#0-4) [6](#0-5) 

**Step 5 — Addresses are never evicted.**

Since these addresses are never dialed, `attempts_count` stays at `0`. `is_connectable()` returns `true` when `last_connected_at_ms == 0 && attempts_count < ADDR_MAX_RETRIES (3)`, so they are never marked non-connectable. [7](#0-6) 

`check_purge()` step 1 only evicts addresses where `is_connectable()` is `false`. Step 2 only evicts from network segments with >4 peers. An attacker using one address per /16 segment bypasses both eviction paths, causing `check_purge()` to return `Err(PeerStoreError::EvictionFailed)` once the store is full. [8](#0-7) 

### Impact Explanation

The peer store has a hard cap of `ADDR_COUNT_LIMIT = 16384` entries. [9](#0-8) 

Once filled with peer-id-less addresses:
- `add_addr()` returns `Err(PeerStoreError::EvictionFailed)` for all new legitimate addresses.
- `fetch_addrs_to_feeler()` and `fetch_addrs_to_attempt()` return empty results.
- The node cannot discover or dial new peers, effectively isolating it from the network.

### Likelihood Explanation

The attack requires only a single connected peer. Each `Nodes` message can carry up to `MAX_ADDR_TO_SEND (1000)` items × `MAX_ADDRS (3)` addresses = 3,000 addresses per message. Filling 16,384 slots requires approximately 6 messages. The attacker needs only to spread addresses across distinct /16 IP segments (e.g., `1.0.0.0/16`, `2.0.0.0/16`, …) to defeat the network-group eviction heuristic. This is trivially achievable with any public IP range. [10](#0-9) 

### Recommendation

1. **Reject peer-id-less addresses at ingestion.** In `add_addr()` or `add_new_addrs()`, require `extract_peer_id(&addr).is_some()` before storing.
2. **Fix the filter semantics.** The `unwrap_or_default()` pattern silently treats "no peer_id" as "already connected." The correct behavior is to reject such addresses at storage time, not silently exclude them at selection time.
3. **Add per-session rate limiting** in the discovery protocol to bound how many addresses a single peer can inject.

### Proof of Concept

```rust
let mut peer_store = PeerStore::default();
// Address without /p2p/ component — accepted by add_addr
let addr: Multiaddr = "/ip4/1.2.3.4/tcp/8114".parse().unwrap();
peer_store.add_addr(addr.clone(), Flags::COMPATIBILITY).unwrap();

// Address is stored
assert_eq!(peer_store.addr_manager().count(), 1);

// But never returned by feeler selection
assert!(peer_store.fetch_addrs_to_feeler(10, |_| true).is_empty());

// And never returned by attempt selection
assert!(peer_store.fetch_addrs_to_attempt(10, Flags::COMPATIBILITY, |_| true).is_empty());

// And is_connectable() stays true forever (never evicted)
let now = ckb_systemtime::unix_time_as_millis();
let info = peer_store.addr_manager().get(&addr).unwrap();
assert!(info.is_connectable(now)); // true, attempts_count == 0
```

### Citations

**File:** network/src/protocols/discovery/mod.rs (L189-205)
```rust
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

**File:** network/src/peer_store/peer_store_impl.rs (L201-209)
```rust
        let filter = |peer_addr: &AddrInfo| {
            filter(peer_addr)
                && extract_peer_id(&peer_addr.addr)
                    .map(|peer_id| !peers.contains_key(&peer_id))
                    .unwrap_or_default()
                && peer_addr
                    .connected(|t| t > addr_expired_ms && t <= now_ms.saturating_sub(DIAL_INTERVAL))
                && required_flags_filter(required_flags, Flags::from_bits_truncate(peer_addr.flags))
        };
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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```
