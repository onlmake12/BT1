### Title
Unbounded `Nodes(announce=true)` Flood Causes Repeated O(n) `check_purge` Invocations and Peer-Store Lock Contention — (`network/src/protocols/discovery/mod.rs`)

### Summary

An unprivileged inbound peer can send an unlimited stream of `Nodes(announce=true)` messages, each containing exactly `ANNOUNCE_THRESHOLD=10` items with up to `MAX_ADDRS=3` addresses each. Because the `received_nodes` guard only blocks duplicate `announce=false` messages, every `announce=true` message is processed unconditionally, triggering 30 calls to `peer_store.add_addr` → `check_purge` per message, with no per-session rate limit. Once the peer store reaches `ADDR_COUNT_LIMIT=16384`, each `check_purge` performs an O(n) scan of all stored addresses while holding the global peer-store mutex, causing CPU exhaustion and lock starvation across all threads that share the store.

---

### Finding Description

**Entry point:** `received()` in `network/src/protocols/discovery/mod.rs`

**Step 1 — Validation passes for exactly-threshold messages.**

`verify_nodes_message` only rejects `announce=true` messages if `items.len() > ANNOUNCE_THRESHOLD` (10), and rejects any item if `addresses.len() > MAX_ADDRS` (3). A message with exactly 10 items × 3 addresses passes both checks. [1](#0-0) 

**Step 2 — The `received_nodes` guard does not apply to `announce=true`.**

The guard condition is `!nodes.announce && state.received_nodes`. For `announce=true`, `!nodes.announce` is `false`, so the condition is always `false` and the message is always processed. `received_nodes` is only set to `true` for `announce=false` messages. There is no counter, timestamp, or token-bucket for announce messages. [2](#0-1) 

**Step 3 — Each message triggers 30 mutex acquisitions and 30 `check_purge` calls.**

`add_new_addrs` iterates over each address individually, calling `with_peer_store_mut` (which acquires `Mutex<PeerStore>`) once per address, and inside each lock scope calls `peer_store.add_addr`, which unconditionally calls `check_purge()` before inserting. [3](#0-2) [4](#0-3) 

**Step 4 — `check_purge` is O(n) when the store is full.**

When `addr_manager.count() >= ADDR_COUNT_LIMIT` (16384), `check_purge` iterates all stored addresses to find eviction candidates, then optionally performs a second full pass grouped by network segment. This work is done while holding the peer-store mutex. [5](#0-4) [6](#0-5) 

**Step 5 — The peer-store mutex is global and shared.**

`NetworkState.peer_store` is a single `Mutex<PeerStore>` shared by all protocols (discovery, identify, feeler, outbound service, dump service). Holding it repeatedly for O(n) work starves all other consumers. [7](#0-6) [8](#0-7) 

**Step 6 — `addr_known` does not filter incoming addresses.**

`state.addr_known.extend(addrs.iter())` is called before `add_new_addrs`, but `addr_known` is only used to suppress *outgoing* announces (in `notify`). It is never consulted to skip processing of *incoming* addresses. The attacker can reuse the same 30 addresses in every message and all 30 will be passed to `add_new_addrs` each time. [9](#0-8) 

---

### Impact Explanation

- **CPU exhaustion:** 10^5 messages × 30 `check_purge` calls × O(16384) iterations = ~4.9×10^10 address comparisons, all on the async network thread.
- **Lock starvation:** The peer-store mutex is acquired 30 times per message. All other subsystems (outbound dialing, feeler, identify, peer-store dump) block waiting for the lock, stalling peer management globally.
- **Crash/stall:** The discovery protocol handler runs in the tentacle async runtime. Saturating it with mutex-contended work can stall all protocol handlers sharing the same executor, effectively halting the node's P2P layer.

---

### Likelihood Explanation

The attack requires only a single TCP connection to an open CKB P2P port (default 8115). No authentication, no PoW, no stake. The attacker needs only to craft valid `Nodes(announce=true)` messages with routable IP addresses (to pass `is_valid_addr`), which is trivial. The attack is reproducible locally and requires no special privileges.

---

### Recommendation

1. **Add a per-session rate limit for `Nodes(announce=true)` messages**, e.g., a token bucket or a counter with a time window, and disconnect peers that exceed it.
2. **Batch the peer-store mutex acquisition** in `add_new_addrs`: acquire the lock once per call rather than once per address.
3. **Throttle `check_purge` invocations**, e.g., only run it when the count exceeds the limit by a margin, or on a timer, rather than on every `add_addr` call.
4. **Apply the `received_nodes` guard symmetrically** or add a separate `received_announce_count` counter with a cap.

---

### Proof of Concept

```python
import socket, struct

# Build a Nodes(announce=true) message with 10 items × 3 routable addrs each
# using the molecule serialization format (packed::Nodes2)
# Connect to CKB P2P port (default 8115), perform tentacle handshake,
# then send in a tight loop:

for _ in range(100_000):
    msg = build_nodes_announce(items=10, addrs_per_item=3,
                               ip_base="8.8.8.")  # routable IPs
    sock.sendall(frame(msg))

# Expected: check_purge invocation count grows linearly with message count;
# victim node's peer-management threads stall; wall-clock processing time
# is unbounded (not O(1) per message).
```

The `build_nodes_announce` function constructs a valid `packed::Nodes2` molecule message with `announce=true`, 10 `Node2` items each containing 3 `/ip4/x.x.x.x/tcp/PORT/p2p/PEERID` multiaddreses using distinct routable IPs, staying within `ANNOUNCE_THRESHOLD=10` and `MAX_ADDRS=3` to pass `verify_nodes_message`.

### Citations

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

**File:** network/src/network.rs (L76-76)
```rust
    pub(crate) peer_store: Mutex<PeerStore>,
```

**File:** network/src/network.rs (L318-323)
```rust
    pub(crate) fn with_peer_store_mut<F, T>(&self, callback: F) -> T
    where
        F: FnOnce(&mut PeerStore) -> T,
    {
        callback(&mut self.peer_store.lock())
    }
```
