### Title
O(n) `check_purge` CPU Amplification via Discovery Addr Relay — (`network/src/peer_store/peer_store_impl.rs`)

---

### Summary

When the peer addr store is at `ADDR_COUNT_LIMIT` (16384) and populated with connectable peers distributed across enough distinct network groups that eviction fails, every call to `add_addr` triggers a full O(n) double-scan of all 16384 entries inside `check_purge`. An unprivileged remote peer can exploit this by continuously sending `DiscoveryMessage::Nodes(announce=true)` messages, each carrying up to 30 new unique addresses, causing sustained CPU amplification with no per-message rate limit in the discovery protocol.

---

### Finding Description

**Entry point — discovery protocol:**

`DiscoveryProtocol::received()` processes incoming `Nodes` messages and calls `add_new_addrs`, which loops over each address and calls `peer_store.add_addr()` for every one. [1](#0-0) [2](#0-1) 

**`add_addr` unconditionally calls `check_purge` before inserting:** [3](#0-2) 

**`check_purge` — two O(n) passes:**

Pass 1 iterates all 16384 entries via `addrs_iter()` looking for non-connectable peers: [4](#0-3) 

If pass 1 finds nothing (all entries connectable), pass 2 iterates all 16384 entries again to build network-group buckets, sort them, and attempt group-based eviction: [5](#0-4) 

If no group has more than 4 peers, eviction fails entirely: [6](#0-5) 

`add_addr` returns `Err` and the new address is **not** inserted, so the store remains at 16384. The next `add_addr` call repeats the full O(2n) scan.

**Newly added addresses are connectable by default:**

`AddrInfo::new` sets `last_connected_at_ms = 0` and `attempts_count = 0`. Under `is_connectable`, this passes all checks (attempts_count < `ADDR_MAX_RETRIES` = 3, attempts_count < `ADDR_MAX_FAILURES` = 10): [7](#0-6) 

**No rate limit on announce messages:**

The only guard on announce `Nodes` messages is a cap of `ANNOUNCE_THRESHOLD` (10) items per message, each with up to `MAX_ADDRS` (3) addresses = 30 `add_addr` calls per message: [8](#0-7) 

There is no per-session or per-time-window rate limit on how frequently a peer may send announce messages.

**`ADDR_COUNT_LIMIT` constant:** [9](#0-8) 

---

### Impact Explanation

Total work per announce message when the store is full and eviction fails:

```
30 addrs/msg × 2 × 16384 iterations = ~983,040 HashMap iterations per message
```

A single connected peer sending announce messages at a sustained rate causes CPU load proportional to message rate, with no bound from the store's own logic. This degrades node responsiveness for block/transaction relay and sync, which share the same async runtime.

---

### Likelihood Explanation

The precondition — filling the store with 16384 connectable addresses from 4096+ distinct /24 networks (≤ 4 per group) — is achievable by the same attacker using the same discovery protocol before the attack phase. Addresses added via `add_addr` are immediately connectable (zero attempts, zero connected time). A single persistent P2P connection is sufficient; no PoW, no privileged role, no Sybil majority is required.

---

### Recommendation

1. **Early-exit guard in `check_purge`**: Track a dirty/full flag so the O(n) scan is not repeated on every call when the previous eviction already failed. Cache the result for a short interval (e.g., 1 second).
2. **Rate-limit `add_addr` calls per session**: Throttle how many addr insertions are processed per peer per time window before invoking `check_purge`.
3. **Amortize eviction**: Decouple eviction from the hot `add_addr` path; run it on a background timer instead of inline.

---

### Proof of Concept

```
1. Connect to victim node via P2P.
2. Send discovery Nodes(announce=false) with 1000 items × 3 addrs each,
   using unique IPs spread across 4096+ /24 networks (≤ 4 per /24).
   Repeat until addr_manager.count() == 16384.
3. In a tight loop, send Nodes(announce=true) with 10 items × 3 unique addrs.
4. Each message triggers 30 × check_purge() calls, each scanning 16384 entries twice.
5. Observe: node CPU spikes proportionally to message rate;
   add_addr returns Err(EvictionFailed) for every call (store stays at 16384).
6. Benchmark: add_addr latency at count=16384 (all connectable, diverse /24s)
   vs count=0 — latency scales linearly with store size.
```

### Citations

**File:** network/src/protocols/discovery/mod.rs (L205-205)
```rust
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

**File:** network/src/protocols/discovery/mod.rs (L347-362)
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

**File:** network/src/peer_store/peer_store_impl.rs (L341-351)
```rust
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
```

**File:** network/src/peer_store/peer_store_impl.rs (L358-393)
```rust
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
```

**File:** network/src/peer_store/peer_store_impl.rs (L399-401)
```rust
            if candidate_peers.is_empty() {
                return Err(PeerStoreError::EvictionFailed.into());
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
