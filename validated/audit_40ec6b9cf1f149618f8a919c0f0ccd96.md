The code confirms the described behavior. Let me trace through all the relevant pieces:

**1. `received_nodes` flag — only set on `announce=false`:** [1](#0-0) 

The guard `if !nodes.announce && state.received_nodes` only fires for `announce=false` duplicates. `announce=true` messages bypass it entirely, and `received_nodes` is never set by them.

**2. `verify_nodes_message` — per-message cap only, no per-session counter:** [2](#0-1) 

`ANNOUNCE_THRESHOLD = 10` caps items per message, `MAX_ADDRS = 3` caps addresses per item. There is no counter tracking how many `announce=true` messages have been received in a session.

**3. Peer store hard cap at `ADDR_COUNT_LIMIT = 16384`:** [3](#0-2) 

**4. `check_purge` eviction — can fail silently:** [4](#0-3) 

When the store is full and no "invalid" (non-connectable) peers exist, and no network group has >4 peers, `check_purge` returns `Err(PeerStoreError::EvictionFailed)`.

**5. `add_new_addrs` silently ignores eviction failure:** [5](#0-4) 

The error is only logged at `debug` level — no disconnection, no misbehavior report.

---

### Title
Unbounded `announce=true` Nodes Messages Allow Single Peer to Flood Peer Store — (`network/src/protocols/discovery/mod.rs`)

### Summary
The `DuplicateFirstNodes` protection only guards against repeated `announce=false` messages. There is no per-session rate limit on `announce=true` Nodes messages. A single peer can send an unlimited number of `Nodes(announce=true)` messages, each injecting up to 30 addresses (10 items × 3 addrs), until the peer store reaches its hard cap of 16,384 entries.

### Finding Description
In `DiscoveryProtocol::received`, the `received_nodes` flag is only set when `!nodes.announce` (line 202–204). The duplicate-check branch (`DuplicateFirstNodes`) is only entered when `!nodes.announce && state.received_nodes` (line 181). Consequently, `announce=true` messages are never counted, never rate-limited per session, and never trigger any misbehavior response regardless of how many are sent.

`verify_nodes_message` enforces a per-message cap of `ANNOUNCE_THRESHOLD = 10` items and `MAX_ADDRS = 3` addresses per item, but imposes no session-level limit on message count. A peer sending N consecutive `Nodes(announce=true, items=[10 nodes × 3 addrs])` messages injects up to `30 × N` addresses before any guard fires.

The peer store's `check_purge` enforces `ADDR_COUNT_LIMIT = 16384`, but eviction can fail silently (`EvictionFailed`), and the error is only debug-logged in `add_new_addrs` with no disconnection or misbehavior penalty applied to the sender.

### Impact Explanation
A single unprivileged peer can fill the entire peer store (16,384 entries) with attacker-controlled addresses using ~547 consecutive `Nodes(announce=true)` messages. This:
- Displaces legitimate peer addresses, degrading the victim node's ability to find honest peers after reconnection or restart.
- Constitutes a concrete first step toward an eclipse attack: if the victim's peer store is saturated with attacker addresses, outbound connection attempts post-restart will preferentially dial attacker-controlled nodes.
- Requires no special privileges — any inbound or outbound P2P session suffices.

### Likelihood Explanation
The attack is trivially executable: establish one P2P session, send a tight loop of crafted `Nodes(announce=true)` messages with valid-looking public IP multiaddrs. No PoW, no key material, no Sybil majority required. The `is_valid_addr` filter (reachability check) is the only barrier, and it is trivially bypassed by using real public IP ranges in the crafted addresses.

### Recommendation
Add a per-session counter for received `announce=true` Nodes messages and apply a session-level cap (e.g., `ANNOUNCE_THRESHOLD` messages per session). When the cap is exceeded, call `misbehave(session, &Misbehavior::TooManyItems {...})` and disconnect, consistent with how other misbehaviors are handled.

### Proof of Concept
```rust
// Pseudocode: attacker sends 600 announce=true messages in one session
for _ in 0..600 {
    let nodes = Nodes {
        announce: true,
        items: (0..10).map(|i| Node {
            addresses: vec![
                format!("/ip4/1.2.{}.{}/tcp/8115/p2p/{}", i, i, PeerId::random().to_base58())
                    .parse().unwrap(),
            ],
            flags: Flags::COMPATIBILITY,
        }).collect(),
    };
    session.send(encode(DiscoveryMessage::Nodes(nodes)));
}
// peer_store.addr_manager().count() approaches ADDR_COUNT_LIMIT (16384)
// legitimate addresses are evicted; no disconnection occurs
```

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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
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
