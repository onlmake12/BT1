### Title
Peer Store Exhaustion via Same-Network Multi-PeerID Flooding Causing Eviction Failure and Node Isolation — (File: `network/src/peer_store/peer_store_impl.rs`)

---

### Summary

The CKB peer store (`AddrManager`) deduplicates entries by full multiaddr (IP + port + peer ID), not by IP:port alone. An attacker connected to a CKB node can flood the peer store with up to 16,384 entries all from the same `/16` network segment but with distinct peer IDs. When the store reaches `ADDR_COUNT_LIMIT`, the `check_purge()` eviction routine silently fails due to an integer-division bug (`take(len / 2)` = `take(0)` when only one network group exists). After that, every call to `add_addr` returns `Err(PeerStoreError::EvictionFailed)`, permanently preventing legitimate peer addresses from entering the store and isolating the node from the network.

---

### Finding Description

**Root cause 1 — No IP:port deduplication in `AddrManager::add()`**

`AddrManager::add()` uses the full multiaddr (including the `/p2p/<PeerId>` component) as its deduplication key:

```rust
if let Some(&id) = self.addr_to_id.get(&addr_info.addr) {
    ...
    return;
}
// insert new entry
self.addr_to_id.insert(addr_info.addr.clone(), id);
```

`AddrInfo::new()` strips only transport-layer decorators (`Ws`, `Wss`, `Memory`, `Tls`) via `base_addr()`, but retains the peer ID:

```rust
pub fn new(addr: Multiaddr, ...) -> Self {
    AddrInfo {
        addr: base_addr(&addr),   // keeps /ip4/.../tcp/.../p2p/...
        ...
    }
}
```

Therefore `/ip4/225.0.0.1/tcp/8115/p2p/PeerId_A` and `/ip4/225.0.0.1/tcp/8115/p2p/PeerId_B` are stored as two independent entries, even though they point to the same physical endpoint. An attacker needs only one IP:port to generate 16,384 distinct multiaddrs. [1](#0-0) [2](#0-1) [3](#0-2) 

---

**Root cause 2 — Integer-division bug in `check_purge()` eviction**

When `addr_manager.count() >= ADDR_COUNT_LIMIT` (16,384), `check_purge()` first tries to evict non-connectable peers. If all attacker entries are fresh (never dialled, `attempts_count = 0`), `is_connectable()` returns `true` for all of them and step 1 finds nothing to remove.

Step 2 groups entries by `/16` network segment and then does:

```rust
let len = peers_by_network_group.len();
...
peers
    .into_iter()
    .take(len / 2)          // integer division
    .flat_map(move |addrs| {
        if addrs.len() > 4 {
            Some(/* evict 2 */)
        } else {
            None
        }
    })
    .flatten()
    .collect()
```

If all 16,384 entries belong to the same `/16` group (e.g., `225.0.0.0/16`), then `len = 1` and `take(1 / 2)` = `take(0)` — the iterator is empty, `candidate_peers` is empty, and the function returns:

```rust
if candidate_peers.is_empty() {
    return Err(PeerStoreError::EvictionFailed.into());
}
``` [4](#0-3) [5](#0-4) 

---

**Root cause 3 — Error silently swallowed at call sites**

Both callers of `add_addr` discard the `EvictionFailed` error without any protective action:

`DiscoveryAddressManager::add_new_addrs()` — only a `debug!` log:
```rust
if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
    debug!("Failed to add discovered address to peer_store {:?} {:?}", err, addr);
}
```

`IdentifyProtocol::add_remote_listen_addrs()` — only an `error!` log:
```rust
if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
    error!("IdentifyProtocol failed to add address to peer store ...");
}
``` [6](#0-5) [7](#0-6) 

---

**Attack delivery via the Discovery protocol**

`verify_nodes_message()` allows up to 1,000 items × 3 addresses = 3,000 addresses per `Nodes` message. Filling 16,384 slots requires only ~6 messages from a single connected peer. The `is_valid_addr` filter rejects loopback/private IPs but accepts public addresses such as `225.0.0.x`. [8](#0-7) 

---

### Impact Explanation

Once the peer store is exhausted and eviction is permanently broken:

- `fetch_addrs_to_feeler` and `fetch_addrs_to_attempt` return only attacker-controlled addresses (or nothing useful, since `fetch_random` deduplicates by IP and returns at most one entry per IP).
- Legitimate peer addresses advertised by honest nodes are silently dropped.
- If the node's existing connections drop (restart, network interruption), it cannot reconnect to honest peers and becomes isolated.
- An isolated node cannot sync headers or blocks, cannot relay transactions, and cannot confirm new transactions.

The impact is **targeted node isolation / inability to sync**, matching the bounty scope of "network not being able to confirm new transactions" for the affected node.

---

### Likelihood Explanation

- **Entry path**: Any single inbound or outbound peer that speaks the Discovery protocol can trigger this. No privileged role is required.
- **Cost**: ~6 crafted `Nodes` messages, each containing 1,000 items with 3 addresses. All addresses can share one real IP (e.g., the attacker's own) with varying ports and freshly generated peer IDs.
- **Persistence**: Once the store is full and eviction is broken, the condition is permanent until the node restarts (the peer store is persisted to disk and reloaded on startup, so the attacker may need to re-flood after a restart).
- **No Sybil requirement**: A single IP in a single `/16` block is sufficient to trigger the `take(0)` eviction failure.

---

### Recommendation

1. **Add IP:port deduplication in `AddrManager::add()`**: Before inserting a new entry, check whether any existing entry shares the same `(IP, port)` pair. If so, update the existing entry's peer ID rather than inserting a duplicate.

2. **Fix the integer-division bug in `check_purge()`**: Replace `take(len / 2)` with `take(len.saturating_add(1) / 2)` (i.e., ceiling division) so that a single-group store still evicts entries:
   ```rust
   .take((len + 1) / 2)
   ```

3. **Add a per-IP cap**: Limit the number of entries per IP address in `AddrManager` (e.g., max 4 per IP), consistent with the eviction threshold already used in `check_purge()`.

---

### Proof of Concept

```
1. Establish a connection to a target CKB node (inbound or outbound).

2. Send repeated Discovery `Nodes` messages (announce=false), each containing
   1000 items × 3 addresses, where every address has the form:
       /ip4/225.0.0.<i>/tcp/<port>/p2p/<freshly_generated_peer_id>
   varying port and peer ID for each entry, keeping all IPs in 225.0.0.0/16.

3. After ~6 messages (≥16384 addresses delivered), the peer store reaches
   ADDR_COUNT_LIMIT.

4. check_purge() is called:
   - Step 1: all entries are connectable (never tried) → no eviction.
   - Step 2: len=1 (single /16 group) → take(0) → candidate_peers empty
             → returns Err(PeerStoreError::EvictionFailed).

5. All subsequent add_addr() calls from honest peers return EvictionFailed
   and are silently dropped.

6. The target node can no longer learn about legitimate peers. When its
   existing connections drop, it cannot reconnect and becomes isolated.
```

### Citations

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

**File:** network/src/peer_store/types.rs (L63-76)
```rust
impl AddrInfo {
    /// Init
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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```

**File:** network/src/peer_store/mod.rs (L92-105)
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

**File:** network/src/protocols/identify/mod.rs (L488-494)
```rust
        self.network_state.with_peer_store_mut(|peer_store| {
            for addr in addrs {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    error!("IdentifyProtocol failed to add address to peer store, address: {}, error: {:?}", addr, err);
                }
            }
        })
```
