Now I have all the code needed to evaluate this claim. Let me trace through the exact logic.

### Title
PeerStore Eviction Deadlock via Crafted /16-Group Flooding Permanently Blocks Peer Discovery — (`network/src/peer_store/peer_store_impl.rs`)

### Summary

`check_purge` has a two-phase eviction strategy that silently deadlocks when the store is filled with exactly ≤4 connectable addresses per network group. An unprivileged attacker can craft this state via P2P `Nodes` discovery messages, causing every subsequent `add_addr` call to return `Err(EvictionFailed)`, which `add_new_addrs` only debug-logs and discards, permanently blocking new peer discovery for as long as the attacker maintains the flood.

---

### Finding Description

**Entrypoint:** A remote peer sends a `DiscoveryMessage::Nodes` message over the P2P discovery protocol.

**Call chain:**

```
received() [discovery/mod.rs:205]
  → addr_mgr.add_new_addrs(session_id, addrs) [mod.rs:205]
    → peer_store.add_addr(addr, flags) [peer_store_impl.rs:75]
      → self.check_purge()? [peer_store_impl.rs:75]
```

**`add_new_addrs` silently swallows the error:** [1](#0-0) 

The `Err` from `add_addr` is only `debug!`-logged; no disconnect, no rate-limit, no propagation.

**`add_addr` calls `check_purge` before inserting:** [2](#0-1) 

**`check_purge` Phase 1** collects non-connectable addresses. A freshly injected address (`last_connected_at_ms=0`, `attempts_count=0`, `last_tried_at_ms=0`) passes `is_connectable` because neither failure threshold is met: [3](#0-2) 

So Phase 1 finds nothing and `candidate_peers.is_empty()` is `true`, entering Phase 2.

**`check_purge` Phase 2** groups addresses by network segment, sorts by group size descending, takes the top `len/2` groups, and only evicts from groups where `addrs.len() > 4`: [4](#0-3) 

If every group has **exactly 4** peers, the condition `addrs.len() > 4` is false for all groups. `candidate_peers` is empty again, and the function returns: [5](#0-4) 

**`ADDR_COUNT_LIMIT` is 16384:** [6](#0-5) 

**Attack construction:**
- 16384 / 4 = **4096 distinct /16 network groups** needed
- Each Nodes message carries up to `MAX_ADDR_TO_SEND=1000` nodes × `MAX_ADDRS=3` addresses = 3000 addresses per message
- ~6 Nodes messages suffice to fill the store
- The attacker crafts addresses from 4096 different /16 subnets (e.g., `1.0.x.x`, `1.1.x.x`, …, `16.15.x.x`), 4 per subnet

Once the store reaches 16384 entries in this configuration, every call to `add_addr` hits `check_purge`, both phases find nothing to evict, and `Err(EvictionFailed)` is returned and silently discarded.

---

### Impact Explanation

The victim node can no longer add any new peer addresses to its store. Peer discovery via the `Nodes` protocol is completely blocked. The node cannot learn about new honest peers, degrading its ability to maintain a healthy outbound peer set and making it progressively easier to eclipse.

The effect persists as long as the attacker maintains connections and periodically re-floods to replace any addresses that eventually become non-connectable (after `ADDR_MAX_RETRIES=3` failed feeler attempts). Given the node's limited feeler connection rate against 16384 fake addresses, the attacker can sustain this state indefinitely with minimal bandwidth.

---

### Likelihood Explanation

- Requires only a standard P2P connection — no privileges, no PoW, no keys
- The crafted address set is trivially constructable (4096 /16 subnets × 4 IPs each)
- The protocol allows up to 1000 nodes per message; ~6 messages fill the store
- The error is silently swallowed, so the victim has no observable signal
- The attacker can use multiple sessions to accelerate filling and to maintain the state

---

### Recommendation

1. **Fix the eviction threshold:** Change `addrs.len() > 4` to `addrs.len() >= 4` (or `>= 1`) so Phase 2 can always evict at least one address per group when the store is full.
2. **Evict from all groups, not just the top half:** The `take(len / 2)` restriction means the bottom half of groups are never candidates, further reducing eviction coverage.
3. **Propagate or rate-limit on `EvictionFailed`:** At minimum, log at `warn!` level and consider disconnecting or rate-limiting the session that triggered the full store condition.
4. **Per-session address admission limit:** Cap how many new addresses a single session can contribute to the store within a time window.

---

### Proof of Concept

```rust
// Pseudocode — fill PeerStore with 4096 /16 groups × 4 addrs each
let mut store = PeerStore::default();
for group in 0u16..4096 {
    let hi = (group >> 8) as u8;
    let lo = (group & 0xff) as u8;
    for host in 1u8..=4 {
        let addr = format!("/ip4/{}.{}.0.{}/tcp/8115/p2p/Qm...", hi, lo, host)
            .parse::<Multiaddr>().unwrap();
        store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
}
assert_eq!(store.addr_manager().count(), 16384);

// Now any new add_addr returns EvictionFailed
let new_addr = "/ip4/200.200.200.1/tcp/8115/p2p/Qm...".parse().unwrap();
let result = store.add_addr(new_addr, Flags::COMPATIBILITY);
assert!(matches!(result, Err(e) if e.to_string().contains("EvictionFailed")));
assert_eq!(store.addr_manager().count(), 16384); // count unchanged
```

### Citations

**File:** network/src/protocols/discovery/mod.rs (L354-360)
```rust
            self.network_state.with_peer_store_mut(|peer_store| {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    debug!(
                        "Failed to add discovered address to peer_store {:?} {:?}",
                        err, addr
                    );
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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```
