### Title
Peer Store Permanently Fillable via Discovery Flood with Crafted Network-Group Distribution — (`network/src/peer_store/peer_store_impl.rs`)

---

### Summary

An unprivileged remote peer can permanently fill the CKB peer store with 16,384 fake, never-dialed addresses arranged so that both eviction phases in `check_purge` produce zero candidates, causing every subsequent `add_addr` call to return `EvictionFailed`. A secondary bug in `AddrManager::add` lets the attacker reset `attempts_count` back to 0 for any address it re-announces, making the condition permanent.

---

### Finding Description

**Phase 1 bypass — `is_connectable` always returns `true` for fresh fake addresses**

`add_addr` always stores discovered addresses with `last_connected_at_ms = 0` and `attempts_count = 0`: [1](#0-0) 

`is_connectable` only returns `false` for a never-connected address when `attempts_count >= ADDR_MAX_RETRIES (3)`: [2](#0-1) 

A fresh fake address (`attempts_count = 0`) always passes this check, so Phase 1 of `check_purge` collects zero candidates: [3](#0-2) 

**Phase 2 bypass — group size threshold `> 4` is never exceeded**

Phase 2 only evicts from groups with **strictly more than 4** members: [4](#0-3) 

If the attacker distributes 16,384 addresses across 4,096 network groups of exactly 4 each (or any distribution where no group exceeds 4), `candidate_peers` is empty and `EvictionFailed` is returned: [5](#0-4) 

**Permanent reset via `AddrManager::add` overwrite**

When the attacker re-announces the same address, `AddrManager::add` overwrites the existing entry because `new.last_connected_at_ms (0) >= existing.last_connected_at_ms (0)` is true, and the new `AddrInfo` always has `attempts_count = 0`: [6](#0-5) 

This resets any accumulated `attempts_count`, preventing the store from self-healing through feeler connection failures.

**Reachable entry point**

`DiscoveryAddressManager::add_new_addrs` is called directly from the P2P discovery message handler for every received `Nodes` message, with no rate limit on how many addresses can be submitted across multiple sessions: [7](#0-6) 

`MAX_ADDR_TO_SEND = 1000` items × `MAX_ADDRS = 3` per item = 3,000 addresses per non-announce message. Six attacker sessions suffice to fill all 16,384 slots.

The identify protocol path is also affected: [8](#0-7) 

---

### Impact Explanation

Once the store is full with the crafted distribution, every call to `add_addr` (from discovery or identify) returns `EvictionFailed`. The node cannot record any new peer addresses. Combined with the `attempts_count` reset trick, the condition is permanent as long as the attacker periodically re-announces the same fake addresses. The node loses the ability to discover new peers and, over time, becomes isolated from the network.

---

### Likelihood Explanation

The attacker needs only a handful of real TCP connections to the victim node (to send discovery messages) and a list of 16,384 globally-routable IP addresses (which they do not need to own). No PoW, no key material, no privileged access is required. The `is_valid_addr` check only filters private/loopback IPs, which is not a meaningful barrier given the size of the public IPv4 space.

---

### Recommendation

1. **Differentiate eviction priority by verification status.** Addresses with `last_connected_at_ms == 0` (never successfully connected) should be evicted before verified addresses. Phase 1 of `check_purge` should treat `last_connected_at_ms == 0 && attempts_count == 0` as a lower-priority candidate, not as connectable.

2. **Fix the `AddrManager::add` overwrite condition.** When an existing entry has a higher `attempts_count`, a re-announcement with `last_connected_at_ms = 0` should not reset it. The update should preserve the maximum `attempts_count`.

3. **Cap per-session address contributions.** Limit how many new addresses a single session can inject into the peer store within a time window.

---

### Proof of Concept

```
1. Attacker opens 6 TCP sessions to the victim CKB node.
2. Each session sends one non-announce Nodes message containing 1000 items × 3 addresses
   = 3,000 addresses, for a total of 18,000 addresses submitted.
3. All addresses are globally-routable IPv4 addresses from 4,096 distinct /16 prefixes,
   4 addresses per prefix (e.g., 1.0.0.1, 1.0.0.2, 1.0.0.3, 1.0.0.4 for prefix 1.0.0.0/16).
4. After 16,384 addresses are stored, check_purge triggers on the next add_addr call:
   - Phase 1: all entries have attempts_count=0 < ADDR_MAX_RETRIES=3 → zero candidates.
   - Phase 2: all 4,096 groups have exactly 4 members, none > 4 → zero candidates.
   - Returns Err(EvictionFailed).
5. Every subsequent add_addr (discovery or identify) returns EvictionFailed.
6. Attacker periodically re-announces the same 16,384 addresses to reset attempts_count,
   preventing self-healing via feeler connection failures.
```

### Citations

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

**File:** network/src/peer_store/peer_store_impl.rs (L341-355)
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

        for key in candidate_peers.iter() {
            self.addr_manager.remove(key);
        }
```

**File:** network/src/peer_store/peer_store_impl.rs (L358-401)
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

            for key in candidate_peers.iter() {
                self.addr_manager.remove(key);
            }

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

**File:** network/src/peer_store/addr_manager.rs (L23-34)
```rust
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
