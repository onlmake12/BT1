The code is concrete and the logic flaw is verifiable. Here is the full analysis:

---

### Title
Peer Store Permanently Fillable via Discovery Flood with Exactly-4-per-Group Addresses, Causing Permanent `EvictionFailed` — (`network/src/peer_store/peer_store_impl.rs`)

### Summary

`check_purge` uses a strictly-greater-than-4 threshold (`addrs.len() > 4`) when deciding which network groups to evict from. An unprivileged remote peer can flood the discovery protocol with exactly `ADDR_COUNT_LIMIT=16384` addresses distributed across 4096 distinct `/16` network groups with exactly 4 addresses each. Under this configuration, no group qualifies for eviction, `candidate_peers` is empty, and `check_purge` returns `Err(PeerStoreError::EvictionFailed)` on every subsequent `add_addr` call. Honest peer addresses are then silently dropped.

---

### Finding Description

**Entry point — discovery protocol:**

`DiscoveryAddressManager::add_new_addrs` is called directly from the `received` handler for `DiscoveryMessage::Nodes`, with no authentication or privilege requirement. [1](#0-0) 

It calls `peer_store.add_addr` for each received address: [2](#0-1) 

Errors are silently swallowed at `debug!` level — no disconnect, no penalty, no retry.

**`add_addr` calls `check_purge` before inserting:** [3](#0-2) 

**`check_purge` — the flawed eviction logic:**

Step 1 collects non-connectable addresses. A freshly-added address has `last_connected_at_ms=0` and `attempts_count=0`. `is_connectable` returns `true` for it because neither the `>= ADDR_MAX_RETRIES` (3) nor `>= ADDR_MAX_FAILURES` (10) threshold is met: [4](#0-3) 

So step 1 yields an empty `candidate_peers` for fresh addresses.

Step 2 groups by network segment and applies the critical threshold: [5](#0-4) 

The condition `if addrs.len() > 4` (line 378) is **strictly greater than 4**. Groups of exactly 4 are never evicted. With 4096 groups × 4 addresses = 16384 entries, every group has exactly 4 members, so `candidate_peers` is empty again, and the function returns: [6](#0-5) 

**`ADDR_COUNT_LIMIT` is confirmed at 16384:** [7](#0-6) 

**Message-size limits do not prevent the attack:**

`MAX_ADDR_TO_SEND=1000` and `MAX_ADDRS=3` per node item means ~3000 addresses per `Nodes` message. Filling 16384 slots requires only ~6 messages, deliverable from a single or a few colluding peers. [8](#0-7) 

---

### Impact Explanation

Once the peer store is full with the attacker's 4-per-group layout, every call to `add_addr` for a new honest peer address hits `check_purge`, which returns `EvictionFailed`. The error is silently discarded. The node cannot learn new peer addresses from the discovery protocol, degrading its ability to find honest peers and making it susceptible to eclipse attacks or network isolation.

---

### Likelihood Explanation

The attack requires only:
- A small number of connections to the victim node (even 1 is sufficient over time)
- IP diversity across 4096 `/16` blocks — the attacker does not need to *own* those IPs; they only need to advertise them via discovery `Nodes` messages
- ~6 `Nodes` messages to fill the store

The addresses remain "connectable" (fresh, `attempts_count=0`) until the victim node actually attempts and fails connections to them. The node does not aggressively dial all 16384 addresses immediately, so the attack window can last hours to days. The attacker can also sustain the attack by continuously sending new diverse addresses from additional peers to replace any that age out.

---

### Recommendation

Change the eviction threshold from strictly greater than (`> 4`) to greater than or equal to (`>= 4`), or reduce it further (e.g., `>= 2`), so that groups of exactly 4 are also candidates for eviction:

```rust
// In check_purge, line 378:
// Before:
if addrs.len() > 4 {
// After:
if addrs.len() >= 4 {
```

Additionally, consider returning an error from `add_new_addrs` or applying a misbehavior penalty when `EvictionFailed` is encountered repeatedly from the same session, rather than silently discarding the error.

---

### Proof of Concept

```rust
// Unit test: fill peer store with 4096 groups × 4 addresses = 16384 entries,
// then assert add_addr returns Err(EvictionFailed).
let mut peer_store = PeerStore::default();
for group in 0u32..4096 {
    for host in 1u32..=4 {
        let ip = std::net::Ipv4Addr::new(
            ((group >> 8) & 0xff) as u8,
            (group & 0xff) as u8,
            0,
            host as u8,
        );
        let addr: Multiaddr = format!("/ip4/{}/tcp/8115", ip).parse().unwrap();
        peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
}
// Store is now at ADDR_COUNT_LIMIT with 4 per /16 group
let new_addr: Multiaddr = "/ip4/200.200.200.1/tcp/8115".parse().unwrap();
let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
assert!(matches!(result, Err(e) if e.to_string().contains("EvictionFailed")));
```

### Citations

**File:** network/src/protocols/discovery/mod.rs (L32-34)
```rust
const MAX_ADDR_TO_SEND: usize = 1000;
// The maximum number addresses in one Nodes item
const MAX_ADDRS: usize = 3;
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
