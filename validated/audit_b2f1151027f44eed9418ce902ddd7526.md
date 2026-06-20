The code is clear enough to make a determination. Let me verify the `check_purge` eviction behavior and `AddrManager` deduplication to complete the analysis.

### Title
Unbounded `announce=true` Nodes Messages Allow Per-Session Peer Store Flooding — (`network/src/protocols/discovery/mod.rs`)

---

### Summary

The `DuplicateFirstNodes` guard only protects against repeated `Nodes(announce=false)` messages. There is no per-session counter or rate limit for `Nodes(announce=true)` messages. A single connected peer can send an unlimited number of `announce=true` messages, each injecting up to 30 addresses into the peer store, until the store's hard cap of 16 384 entries is reached and eviction fails silently.

---

### Finding Description

In `DiscoveryProtocol::received`, the `received_nodes` flag is set only when `announce == false`: [1](#0-0) 

The guard at line 181 (`if !nodes.announce && state.received_nodes`) is never entered for `announce=true` messages, so `received_nodes` stays `false` indefinitely. The only per-message check is `verify_nodes_message`, which caps a single message at `ANNOUNCE_THRESHOLD` (10) items and `MAX_ADDRS` (3) addresses per item — 30 addresses maximum per message — but imposes no limit on how many such messages may arrive per session: [2](#0-1) 

Every accepted message calls `add_new_addrs`, which calls `peer_store.add_addr` for each address: [3](#0-2) 

`add_addr` calls `check_purge` before inserting. When the store reaches `ADDR_COUNT_LIMIT = 16 384`, eviction is attempted: [4](#0-3) [5](#0-4) 

The eviction strategy first removes non-connectable peers, then removes peers from network segments with >4 entries. If the attacker spreads fake addresses across many distinct /24 subnets (≤4 per subnet), neither eviction pass removes them, and `check_purge` returns `Err(PeerStoreError::EvictionFailed)`. That error is silently swallowed in `add_new_addrs` (only a `debug!` log). Legitimate peer addresses submitted after this point are silently dropped.

`AddrManager.add` deduplicates by exact address: [6](#0-5) 

So the attacker must supply unique addresses, but generating 16 384 unique `(public-IP, port, peer-id)` tuples is trivial.

---

### Impact Explanation

A single connected peer can fill the 16 384-entry peer store with attacker-controlled fake addresses in ~547 messages (16 384 / 30). Once full and with eviction defeated, no new legitimate peer addresses can be stored. The victim node's peer discovery is permanently degraded for the lifetime of the peer store, making it unable to learn about honest peers and creating conditions for a subsequent eclipse attack.

---

### Likelihood Explanation

Any peer that completes the P2P handshake can execute this. No special privilege, hashpower, or Sybil capability is required. The attacker needs only one TCP connection and the ability to send ~550 well-formed discovery messages. The `is_valid_addr` filter requires publicly routable IPs, but the attacker does not need to *own* those IPs — they only need to advertise them as peer addresses.

---

### Recommendation

Add a per-session counter for `announce=true` Nodes messages (or a total per-session address budget) and disconnect/misbehave when it is exceeded. A simple approach is to track `received_announce_count: usize` in `SessionState` and trigger `Misbehavior::TooManyItems` (or a new `ExcessiveAnnounce` variant) once it surpasses a small threshold (e.g., `ANNOUNCE_THRESHOLD` messages per session). Alternatively, apply the same "only accept once" semantics to announce messages by capping the total addresses accepted per session across both announce and non-announce paths.

---

### Proof of Concept

```
1. Connect to victim node (session established, received_nodes = false).
2. In a loop (N = 600 iterations):
     Send DiscoveryMessage::Nodes { announce: true, items: [10 nodes × 3 unique public addrs] }
     // verify_nodes_message passes (10 ≤ ANNOUNCE_THRESHOLD, 3 ≤ MAX_ADDRS)
     // received_nodes check skipped (announce=true)
     // 30 addresses added to peer store per iteration
3. After ~547 iterations peer_store.addr_manager.count() == 16384.
4. check_purge returns EvictionFailed (addresses spread across >4096 distinct /24 subnets).
5. Any subsequent add_addr from a legitimate peer silently fails.
6. Optionally send Nodes(announce=false) — received_nodes is set to true, but damage is done.
```

Unit-test assertion: after step 4, `peer_store.addr_manager().count() == ADDR_COUNT_LIMIT` and all stored addresses belong to the attacker's fake set.

### Citations

**File:** network/src/protocols/discovery/mod.rs (L181-204)
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
```

**File:** network/src/protocols/discovery/mod.rs (L266-300)
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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```

**File:** network/src/peer_store/peer_store_impl.rs (L327-403)
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
