Audit Report

## Title
Unbounded `announce=true` Nodes Flood Bypasses `DuplicateFirstNodes` Guard, Filling PeerStore — (`network/src/protocols/discovery/mod.rs`)

## Summary

The `DuplicateFirstNodes` guard in the discovery protocol is structurally bypassed for all `announce=true` messages because `received_nodes` is only set on the `announce=false` branch. An unauthenticated remote peer can send an unlimited stream of `Nodes{announce: true}` messages, each carrying up to 10 × 3 = 30 unique addresses, with no per-session rate limit. This unconditionally fills the global `PeerStore` to `ADDR_COUNT_LIMIT=16384` with attacker-controlled fake addresses, triggering repeated O(n) `check_purge` eviction scans and permanently degrading peer discovery for the victim node.

## Finding Description

**Guard only covers `announce=false`:**

The `DuplicateFirstNodes` misbehavior check at line 181 fires only when `!nodes.announce && state.received_nodes`: [1](#0-0) 

`received_nodes` is set to `true` only inside the `if !nodes.announce` branch: [2](#0-1) 

For every `announce=true` message, the `else` branch is always taken, `received_nodes` is never set, and `add_new_addrs` is called unconditionally. The guard is structurally unreachable for any `announce=true` message.

**`SessionState` has no announce message counter:**

`SessionState` tracks only two boolean flags — `received_get_nodes` and `received_nodes` — with no counter for announce messages: [3](#0-2) 

**`verify_nodes_message` is per-message only, not per-session:**

The function caps each individual message at `ANNOUNCE_THRESHOLD=10` items and `MAX_ADDRS=3` addresses per item. These are structural checks, not rate limits. A compliant attacker sends messages exactly at the limit (30 addrs/msg) and is never disconnected: [4](#0-3) 

**`add_new_addrs` inserts directly into the global PeerStore:** [5](#0-4) 

**`add_addr` calls `check_purge` on every insertion:** [6](#0-5) 

**`check_purge` is O(n) and triggered at `ADDR_COUNT_LIMIT=16384`:** [7](#0-6) [8](#0-7) 

**`AddrManager.add` deduplicates by exact address**, so the attacker must supply unique addresses — trivially achievable by varying IP/port across the public IPv4 space: [9](#0-8) 

**`misbehave` stub always disconnects but is never invoked for announce rate violations:** [10](#0-9) 

## Impact Explanation

This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

- The global `PeerStore` fills to `ADDR_COUNT_LIMIT=16384` with attacker-controlled fake addresses.
- Every subsequent `add_addr` call triggers `check_purge`, an O(n) scan over all 16384 entries.
- When the attacker spreads fake addresses across many /16 subnets (each group ≤4 entries), the network-group eviction strategy fails entirely and `check_purge` returns `Err(PeerStoreError::EvictionFailed)`, permanently blocking new legitimate address insertion.
- `fetch_addrs_to_feeler` and `fetch_addrs_to_attempt` return only attacker-controlled addresses that will never connect, starving the node of real peers.
- A small botnet of a few dozen peers can sustain this flood across the entire CKB network simultaneously, causing widespread peer discovery degradation with minimal cost.

## Likelihood Explanation

- Requires only a single unauthenticated TCP connection — no PoW, no authentication, no privileged role.
- ~547 messages (16384 / 30) fill the store; at typical P2P message rates this completes in seconds.
- The `is_valid_addr` filter requires public IPs in production, but the public IPv4 space is large enough to trivially supply 16384 unique addresses.
- A botnet of a few dozen peers can sustain the flood indefinitely across the entire network.

## Recommendation

1. **Add an `announce_msg_count` counter to `SessionState`** and trigger `Misbehavior` (leading to disconnect) after exceeding a per-session budget (e.g., N announce messages per minute).
2. **Extend `received_nodes` semantics** or introduce a separate rate-limit counter for `announce=true` messages, mirroring the existing `received_nodes` guard for `announce=false`.
3. **Apply per-source-IP insertion limits** in `add_addr` to bound how many entries a single peer can contribute to the store.
4. **Implement graduated scoring in `misbehave`** rather than the current always-disconnect stub, so rate-limit violations can be tracked before disconnection.

## Proof of Concept

```
1. Connect to victim node (inbound session).
2. In a loop:
     Send DiscoveryMessage::Nodes {
         announce: true,
         items: [Node { addresses: [unique_public_ip_N:port], flags: COMPATIBILITY }; 10]
     }
   where each message uses 10 fresh unique public IPs.
3. After ~547 messages, assert peer_store.addr_manager().count() == 16384.
4. Observe check_purge invoked on every subsequent add_addr call.
5. Spread addresses across distinct /16 subnets (≤4 per group) to trigger
   EvictionFailed, permanently blocking new legitimate address insertion.
6. Observe fetch_addrs_to_feeler returning only attacker-controlled addresses.
```

The `received_nodes` flag is set at line 203 only inside `if !nodes.announce`, so the guard at line 181 is structurally unreachable for any `announce=true` message, making this exploit unconditionally reachable from any unauthenticated peer session. [11](#0-10)

### Citations

**File:** network/src/protocols/discovery/mod.rs (L181-205)
```rust
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

**File:** network/src/protocols/discovery/mod.rs (L365-373)
```rust
    fn misbehave(&mut self, session: &SessionContext, behavior: &Misbehavior) -> MisbehaveResult {
        error!(
            "DiscoveryProtocol detects abnormal behavior, session: {:?}, behavior: {:?}",
            session, behavior
        );

        // FIXME:
        MisbehaveResult::Disconnect
    }
```

**File:** network/src/protocols/discovery/state.rs (L28-37)
```rust
pub struct SessionState {
    // received pending messages
    pub(crate) addr_known: AddrKnown,
    // FIXME: Remote listen address, resolved by id protocol
    pub(crate) remote_addr: RemoteAddress,
    last_announce: Option<Instant>,
    pub(crate) announce_multiaddrs: Vec<(Multiaddr, Flags)>,
    pub(crate) received_get_nodes: bool,
    pub(crate) received_nodes: bool,
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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```

**File:** network/src/peer_store/addr_manager.rs (L22-34)
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
```
