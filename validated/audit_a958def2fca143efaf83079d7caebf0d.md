The code is fully readable. Here is the complete analysis:

---

### Title
Unbounded `announce=true` Nodes Flood Bypasses `DuplicateFirstNodes` Guard, Filling PeerStore — (`network/src/protocols/discovery/mod.rs`)

### Summary

An unprivileged remote peer can send an unlimited stream of `Nodes{announce=true}` messages, each carrying up to 10 nodes × 3 addresses, with no per-session rate limit. The `DuplicateFirstNodes` guard is structurally bypassed because `received_nodes` is only set for `announce=false` messages. Each valid message unconditionally calls `add_new_addrs`, which inserts up to 30 fresh addresses into the global `PeerStore`. A single attacker session can fill the store to `ADDR_COUNT_LIMIT=16384`, triggering repeated O(n) `check_purge` eviction storms and permanently degrading peer discovery.

### Finding Description

**Guard logic — `received_nodes` only covers `announce=false`:**

In `received`, the `DuplicateFirstNodes` guard fires only when `!nodes.announce && state.received_nodes`: [1](#0-0) 

`received_nodes` is set to `true` only on the `!nodes.announce` branch: [2](#0-1) 

For every `announce=true` message, the `else` branch is always taken, `received_nodes` is never set, and `add_new_addrs` is called unconditionally: [3](#0-2) 

**Per-message size limits do not rate-limit the session:**

`verify_nodes_message` caps each individual message at `ANNOUNCE_THRESHOLD=10` items and `MAX_ADDRS=3` addresses per item: [4](#0-3) 

These are per-message structural checks, not per-session or per-time-window rate limits. A compliant attacker sends messages that are exactly at the limit (10 × 3 = 30 addrs/msg) and is never disconnected.

**`add_new_addrs` inserts directly into the global PeerStore:** [5](#0-4) 

**`add_addr` calls `check_purge` on every insertion:** [6](#0-5) 

**`check_purge` is O(n) and triggered at `ADDR_COUNT_LIMIT=16384`:** [7](#0-6) [8](#0-7) 

**`AddrManager.add` deduplicates by exact address**, so the attacker must supply unique addresses — trivially achievable by varying the IP/port across the large public IPv4 space: [9](#0-8) 

**`addr_known` bloom filter does not block peer store insertion** — it only prevents the local node from re-advertising those addresses back to the same peer. The attacker controls what addresses it sends and can always supply fresh ones not in the filter. [10](#0-9) 

### Impact Explanation

- The global `PeerStore` fills to `ADDR_COUNT_LIMIT=16384` with attacker-controlled fake addresses.
- Every subsequent `add_addr` call triggers `check_purge`, an O(n) scan over all 16384 entries, causing CPU overhead proportional to the store size.
- Legitimate peer addresses discovered by honest nodes are evicted by the network-group eviction strategy, which the attacker can game by spreading fake addresses across many /16 subnets.
- Peer discovery for the entire node is degraded: `fetch_addrs_to_feeler` and `fetch_addrs_to_attempt` return attacker-controlled addresses that will never connect, starving the node of real peers.

### Likelihood Explanation

- Requires only a single TCP connection to the victim node — no authentication, no PoW, no privileged role.
- ~547 messages (16384 / 30) fill the store; at typical P2P message rates this completes in seconds.
- A small botnet of a few dozen peers can sustain the flood indefinitely across the entire network.

### Recommendation

1. **Track announce message count per session** and disconnect after a threshold (e.g., N announce messages per minute per session).
2. **Extend `received_nodes` semantics** or introduce a separate `announce_msg_count` counter in `SessionState` that triggers `Misbehavior` after exceeding a per-session budget.
3. **Apply per-source-IP insertion limits** in `add_addr` to bound how many entries a single peer can contribute to the store.
4. **Remove the `// FIXME:` stub** in `misbehave` and implement graduated scoring rather than always-disconnect, so rate-limit violations can be tracked before disconnection.

### Proof of Concept

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
5. Observe fetch_addrs_to_feeler returning only attacker-controlled addresses.
```

The `received_nodes` flag is set at line 203 only inside `if !nodes.announce`, so the guard at line 181 is structurally unreachable for any `announce=true` message, making this exploit unconditionally reachable from any unauthenticated peer session. [11](#0-10) [12](#0-11)

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

**File:** network/src/protocols/discovery/state.rs (L36-36)
```rust
    pub(crate) received_nodes: bool,
```
