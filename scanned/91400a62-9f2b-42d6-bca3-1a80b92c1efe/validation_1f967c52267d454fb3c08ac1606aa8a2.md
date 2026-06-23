### Title
Peer Store Permanent DoS via Crafted /16-Subnet-Diverse Addresses — (`network/src/peer_store/peer_store_impl.rs`)

### Summary

The `check_purge` second-phase eviction uses a strict `> 4` threshold. An attacker who fills `addr_manager` with exactly 4 addresses per /16 subnet (totaling 16384 entries) causes every subsequent `add_addr` call to return `PeerStoreError::EvictionFailed`, permanently preventing honest peer addresses from being stored.

---

### Finding Description

**Phase 1 of `check_purge`** collects addresses where `is_connectable` returns `false`: [1](#0-0) 

For attacker-supplied addresses with `last_connected_at_ms=0` and `attempts_count=0`, `is_connectable` returns `true` because neither the `>= ADDR_MAX_RETRIES` (3) nor the `>= ADDR_MAX_FAILURES` (10) condition is met: [2](#0-1) 

Phase 1 therefore produces zero candidates and falls through to Phase 2.

**Phase 2** groups addresses by network segment, sorts by descending group size, takes the top half, and evicts only from groups where `addrs.len() > 4`: [3](#0-2) 

With 16384 addresses spread across 4096 /16 subnets at exactly 4 per subnet:
- `len = 4096`, `take(len / 2)` = `take(2048)`
- Every group has `len == 4`, so `4 > 4` is **false** for all groups
- `candidate_peers` is empty → `Err(PeerStoreError::EvictionFailed)` is returned [4](#0-3) 

**`add_addr` propagates this error** via `?`: [5](#0-4) 

**The discovery protocol silently swallows the error** at `debug` log level: [6](#0-5) 

The constant `ADDR_COUNT_LIMIT = 16384` is the exact capacity that must be filled: [7](#0-6) 

---

### Impact Explanation

Once the store is saturated with attacker-controlled entries, `add_addr` permanently returns `EvictionFailed`. Honest peer addresses relayed via discovery are silently dropped. `fetch_addrs_to_attempt` and `fetch_addrs_to_feeler` can only return attacker-controlled addresses (which will fail to connect), so as existing connections drop naturally, the node cannot replace them with honest peers. Over time this produces outbound connection starvation and sync isolation.

---

### Likelihood Explanation

The attacker needs only one or a few connected discovery peers to relay crafted `Nodes` messages. The discovery protocol accepts addresses from any connected peer; `is_valid_addr` only checks `is_reachable` (globally routable), not actual reachability. With `MAX_ADDR_TO_SEND = 1000` per message, filling 16384 slots requires ~17 messages, achievable in minutes. No privileged access, PoW, or key material is required.

---

### Recommendation

Change the eviction threshold from strictly greater-than to greater-than-or-equal:

```rust
// peer_store_impl.rs, inside check_purge Phase 2
if addrs.len() >= 4 {   // was: > 4
```

This ensures groups at the per-subnet limit are also eligible for eviction. Additionally, consider rate-limiting the number of addresses accepted per discovery session and per source /16 subnet during ingestion in `add_new_addrs`.

---

### Proof of Concept

```rust
// Pseudocode unit test
let mut peer_store = PeerStore::default();
// Fill with 4096 /16 subnets × 4 addresses each = 16384 total
for subnet in 0u16..4096 {
    for host in 1u8..=4 {
        let ip = format!("{}.{}.1.{}", subnet >> 8, subnet & 0xff, host);
        let addr: Multiaddr = format!("/ip4/{}/tcp/8115", ip).parse().unwrap();
        peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
}
assert_eq!(peer_store.addr_manager().count(), 16384);

// Any new address now fails permanently
let new_addr: Multiaddr = "/ip4/200.200.200.1/tcp/8115".parse().unwrap();
let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
assert!(matches!(result, Err(Error::PeerStore(PeerStoreError::EvictionFailed))));
assert_eq!(peer_store.addr_manager().count(), 16384); // unchanged
```

### Citations

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
