Let me look at the `check_purge` eviction logic and `AddrManager` in more detail to complete the analysis.

### Title
Unbounded `Nodes(announce=true)` Flooding Exhausts Peer Store, Causing Sustained Peer Discovery DoS — (`network/src/protocols/discovery/mod.rs`)

---

### Summary

An unprivileged remote peer can send an unlimited number of `Nodes(announce=true)` messages, each carrying exactly `ANNOUNCE_THRESHOLD` (10) items with `MAX_ADDRS` (3) addresses per item. Every such message passes `verify_nodes_message` without triggering misbehavior, and `add_new_addrs` is called with 30 unique addresses per message. There is no per-session rate limit or counter on `announce=true` messages. After ~547 messages the peer store reaches `ADDR_COUNT_LIMIT` (16 384). If the attacker uses addresses spread across ≥4 097 distinct /16 subnets (≤4 per group), `check_purge` cannot evict any entry and returns `EvictionFailed`. That error is silently swallowed in `add_new_addrs`, so all subsequent legitimate address insertions fail. The attacker can sustain this indefinitely, keeping the store full of fake addresses and blocking honest peer discovery.

---

### Finding Description

**Entry point — no rate limit on `announce=true` messages**

`received()` handles `DiscoveryMessage::Nodes` by first calling `verify_nodes_message`: [1](#0-0) 

`verify_nodes_message` only rejects messages where `items.len() > ANNOUNCE_THRESHOLD` (10) or any item has `addresses.len() > MAX_ADDRS` (3): [2](#0-1) 

A message with exactly 10 items × 3 addresses passes every check. There is no frequency counter, no per-session budget, and no timestamp gate on `announce=true` messages.

**The duplicate-message guard only covers `announce=false`**

The only idempotency guard is: [3](#0-2) 

Because `nodes.announce` is `true`, the `!nodes.announce && state.received_nodes` branch is never entered. `add_new_addrs` is called unconditionally for every `announce=true` message.

**`add_new_addrs` silently swallows `EvictionFailed`** [4](#0-3) 

`peer_store.add_addr()` calls `check_purge()`. When the store is full and eviction fails, `Err(PeerStoreError::EvictionFailed)` is returned but only debug-logged; the caller never disconnects the peer or stops processing.

**`check_purge` fails when addresses are spread across diverse subnets** [5](#0-4) 

Step 1 evicts non-connectable peers. Fresh attacker addresses have `last_connected_at_ms=0` and `attempts_count=0`, so `is_connectable()` returns `true` — they are not evicted: [6](#0-5) 

Step 2 evicts only from network groups with `> 4` peers. If the attacker distributes addresses across ≥4 097 distinct /16 subnets (≤4 per group), no group qualifies and `candidate_peers` is empty, triggering: [7](#0-6) 

**`ADDR_COUNT_LIMIT` and the constants** [8](#0-7) [9](#0-8) 

---

### Impact Explanation

Once the store is saturated with 16 384 fake addresses from diverse subnets:

- Every call to `peer_store.add_addr()` for a legitimate discovered address returns `EvictionFailed` and is silently dropped.
- The node's peer store contains only attacker-controlled fake addresses. Outbound connection attempts to those addresses fail, incrementing `attempts_count`. After `ADDR_MAX_RETRIES` (3) failures the address becomes non-connectable and is evicted — but the attacker can immediately refill the freed slot by sending another `Nodes(announce=true)` message.
- Honest peer discovery is continuously blocked: the node cannot accumulate real peer addresses, degrading its ability to find new honest peers and recover from network partitions.
- Existing live connections are unaffected, so block/transaction relay through already-established sessions continues. The impact is specifically sustained peer-discovery DoS.

---

### Likelihood Explanation

- Requires only a standard P2P connection — no privilege, no PoW, no key material.
- ~547 messages of ~400 bytes each (~218 KB total) suffice to fill the store.
- IPv4 has 65 536 possible /16 subnets; distributing 16 384 addresses at ≤4 per subnet is trivially achievable.
- The attack is self-sustaining: the attacker sends a trickle of new messages to replace evicted entries, keeping the store perpetually full.
- No existing guard (misbehavior scoring, ban list, connection limit) prevents this from a single connected peer.

---

### Recommendation

1. **Add a per-session rate limit on `announce=true` messages** — e.g., allow at most N announce messages per session per time window (analogous to Bitcoin's `ADDR` message rate limit).
2. **Cap the total addresses accepted per session** — track a per-`SessionState` counter and stop calling `add_new_addrs` once the budget is exhausted.
3. **Treat `EvictionFailed` as a signal to disconnect or penalize the sending peer** rather than silently swallowing it.
4. **Improve `check_purge` eviction** — consider evicting the oldest-seen (lowest `last_connected_at_ms`) entries regardless of network group when all other strategies fail, to prevent a full-store deadlock.

---

### Proof of Concept

```rust
// Pseudocode — maps directly to production types
let mut peer_store = PeerStore::default();
let mut subnet = 1u32;
let mut port = 1u16;

for _ in 0..600 {                          // 600 Nodes(announce=true) messages
    let mut items = Vec::new();
    for _ in 0..10 {                       // ANNOUNCE_THRESHOLD items
        let mut addresses = Vec::new();
        for _ in 0..3 {                    // MAX_ADDRS addresses per item
            // Use a fresh /16 subnet each time to defeat group eviction
            let ip = format!("{}.{}.0.1", subnet >> 8, subnet & 0xff);
            subnet += 1;
            let addr: Multiaddr = format!("/ip4/{}/tcp/{}/p2p/{}", ip, port, PeerId::random().to_base58()).parse().unwrap();
            port = port.wrapping_add(1);
            addresses.push(addr);
        }
        items.push(Node { addresses, flags: Flags::COMPATIBILITY });
    }
    // verify_nodes_message passes: 10 items, 3 addrs each
    assert!(verify_nodes_message(&Nodes { announce: true, items: items.clone() }).is_none());
    for node in items {
        for addr in node.addresses {
            let _ = peer_store.add_addr(addr, Flags::COMPATIBILITY);
        }
    }
}

// After 600 messages × 30 addrs = 18 000 insertions (store capped at 16 384)
assert!(peer_store.addr_manager().count() >= ADDR_COUNT_LIMIT);

// Now a legitimate address cannot be added
let honest_addr: Multiaddr = format!("/ip4/8.8.8.1/tcp/8115/p2p/{}", PeerId::random().to_base58()).parse().unwrap();
let result = peer_store.add_addr(honest_addr, Flags::COMPATIBILITY);
assert!(matches!(result, Err(e) if e.to_string().contains("EvictionFailed")));
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

**File:** network/src/protocols/discovery/mod.rs (L170-178)
```rust
                    DiscoveryMessage::Nodes(nodes) => {
                        if let Some(misbehavior) = verify_nodes_message(&nodes)
                            && check(misbehavior)
                        {
                            if context.disconnect(session.id).await.is_err() {
                                debug!("Disconnect {:?} msg failed to send", session.id)
                            }
                            return;
                        }
```

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
