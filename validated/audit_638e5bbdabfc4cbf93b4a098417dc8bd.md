Audit Report

## Title
Unbounded `announce=true` Nodes Messages Allow Single Peer to Flood Peer Store — (`network/src/protocols/discovery/mod.rs`)

## Summary
The `DuplicateFirstNodes` guard at line 181 only fires for `announce=false` messages; `announce=true` messages are never counted per session, never rate-limited, and never trigger a misbehavior response. A single peer can send an unlimited stream of `Nodes(announce=true)` messages, each injecting up to 30 unique addresses, until the peer store reaches its hard cap of 16,384 entries. Eviction failure is silently swallowed and no disconnection occurs, enabling peer store saturation as a concrete eclipse-attack prerequisite.

## Finding Description
In `DiscoveryProtocol::received`, the branch at line 181 is `if !nodes.announce && state.received_nodes`, so the `DuplicateFirstNodes` check is entirely skipped for `announce=true` messages. [1](#0-0) 

`received_nodes` is only set when `!nodes.announce` (lines 202–204), so it is never incremented by announce messages and provides no session-level counter for them. [2](#0-1) 

`verify_nodes_message` enforces `ANNOUNCE_THRESHOLD = 10` items per message and `MAX_ADDRS = 3` addresses per item, but imposes no limit on how many such messages may arrive in one session. [3](#0-2) 

`add_new_addrs` passes each address through `is_valid_addr` (a public-IP reachability check, trivially satisfied with real public ranges) and then calls `peer_store.add_addr`. Errors are only `debug!`-logged; no misbehavior is reported and no disconnection is triggered. [4](#0-3) 

`AddrManager.add` deduplicates by exact address, so the attacker must use unique addresses per message — trivially achieved with distinct public IPs. [5](#0-4) 

`check_purge` enforces `ADDR_COUNT_LIMIT = 16384` but can return `Err(PeerStoreError::EvictionFailed)` when no non-connectable peers exist and no network group exceeds 4 entries — exactly the condition an attacker spreading addresses across many /16 subnets creates. [6](#0-5) [7](#0-6) 

## Impact Explanation
A single unprivileged peer can saturate the peer store (16,384 entries) with attacker-controlled addresses using approximately 547 consecutive `Nodes(announce=true)` messages (30 unique addresses each). This displaces legitimate peer addresses. After the victim node restarts or loses its current connections, outbound dials will preferentially target attacker-controlled addresses, constituting a concrete eclipse-attack prerequisite. This matches the **High** bounty impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"* — one session, no PoW, no key material, no Sybil majority required.

## Likelihood Explanation
The attack requires only a standard P2P session (inbound or outbound). The attacker crafts messages with valid-looking public IP multiaddrs, sends them in a tight loop, and the victim node applies no rate limit, no session counter, and no misbehavior penalty. The `is_valid_addr` filter is the sole barrier and is bypassed by using real public IP ranges.

## Recommendation
Add a per-session counter for received `announce=true` Nodes messages in `SessionState`. When the counter exceeds a session-level cap (e.g., `ANNOUNCE_THRESHOLD` messages per session), call `misbehave(session, &Misbehavior::TooManyItems { ... })` and disconnect, consistent with how `DuplicateGetNodes` and `DuplicateFirstNodes` are handled. Alternatively, apply a token-bucket or sliding-window rate limit on the number of addresses accepted per session.

## Proof of Concept
```rust
// Attacker sends 600 announce=true messages in one session.
// Each message carries 10 items × 3 unique public-IP addresses = 30 addrs.
// Total injected: up to 18,000 addresses (capped at ADDR_COUNT_LIMIT = 16,384).
for i in 0u32..600 {
    let nodes = Nodes {
        announce: true,
        items: (0..10).map(|j| Node {
            addresses: vec![
                format!("/ip4/{}.{}.{}.{}/tcp/8115/p2p/{}",
                    1 + (i / 256) % 254,
                    i % 256,
                    j,
                    (i + j) % 254 + 1,
                    PeerId::random().to_base58())
                .parse().unwrap(),
            ],
            flags: Flags::COMPATIBILITY,
        }).collect(),
    };
    session.send(encode(DiscoveryMessage::Nodes(nodes)));
}
// Expected: peer_store.addr_manager().count() → 16,384
// No disconnection, no misbehavior score, no log above DEBUG level.
```

### Citations

**File:** network/src/protocols/discovery/mod.rs (L181-188)
```rust
                            if !nodes.announce && state.received_nodes {
                                warn!("Nodes (announce=false) message received");
                                if check(Misbehavior::DuplicateFirstNodes)
                                    && context.disconnect(session.id).await.is_err()
                                {
                                    debug!("Disconnect {:?} msg failed to send", session.id)
                                }
                            } else {
```

**File:** network/src/protocols/discovery/mod.rs (L202-204)
```rust
                                if !nodes.announce {
                                    state.received_nodes = true;
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
