Now I have all the code needed for a thorough analysis. Let me trace through every step of the claimed exploit path.

**Constants confirmed:**
- `ADDR_COUNT_LIMIT = 16384` [1](#0-0) 
- `ADDR_MAX_RETRIES = 3`, `ADDR_MAX_FAILURES = 10` [2](#0-1) 

**`is_connectable` with `last_connected_at_ms=0`, `attempts_count=0` (what `add_addr` actually creates):**
- `tried_in_last_minute`: false (last_tried_at_ms=0, now_ms >> 60000)
- `last_connected_at_ms==0 && attempts_count >= ADDR_MAX_RETRIES`: `true && 0>=3` → false
- `now_ms.saturating_sub(0) > ADDR_TIMEOUT_MS && attempts_count >= ADDR_MAX_FAILURES`: `now_ms>7days && 0>=10` → second condition false
- Returns **true** [3](#0-2) 

Note: the question says `attempts_count=2`, but `add_addr` always creates entries with `attempts_count=0`. The exploit works identically with `attempts_count=0` — the first eviction path still finds zero candidates. [4](#0-3) 

**Second eviction path — the critical flaw:**

The second path groups by network group (IPv4 /16 subnet), sorts descending by group size, takes the top `len/2` groups, and only evicts from groups with **strictly more than 4** peers: [5](#0-4) 

With 4096 groups × 4 addresses each = 16384 total:
- `len = 4096`, `len/2 = 2048` groups examined
- All groups have exactly 4 → `addrs.len() > 4` is false for every group
- `candidate_peers` is empty → `return Err(PeerStoreError::EvictionFailed)`

**Attacker entry point — P2P discovery protocol:**

`DiscoveryAddressManager::add_new_addrs` calls `peer_store.add_addr` for each received address, only filtering non-reachable IPs. Errors are silently swallowed as debug logs: [6](#0-5) 

The attacker connects to the victim and sends `Nodes` discovery messages containing addresses from 4096 distinct IPv4 /16 subnets (e.g., `1.0.x.y`, `1.1.x.y`, …, `16.15.x.y`), 4 addresses per subnet. All pass `is_reachable` since they are globally routable. The attacker does not need to own these IPs.

Network group computation confirms /16 grouping: [7](#0-6) 

---

### Title
Peer Store Exhaustion via Crafted Discovery Addresses Causes Permanent `EvictionFailed` — (`network/src/peer_store/peer_store_impl.rs`)

### Summary
An unprivileged attacker can permanently block a CKB node's peer store from accepting any new addresses by flooding it with 16384 crafted addresses distributed across exactly 4096 IPv4 /16 subnets (4 per subnet). Both eviction paths in `check_purge` produce zero candidates, causing every subsequent `add_addr` call to return `EvictionFailed`.

### Finding Description
`check_purge` has two eviction strategies:

**Path 1** — evict non-connectable addresses: All attacker-injected entries have `last_connected_at_ms=0` and `attempts_count=0`. `is_connectable` returns `true` for all of them (neither `ADDR_MAX_RETRIES` nor `ADDR_MAX_FAILURES` threshold is reached), so zero candidates are found.

**Path 2** — evict from over-represented network groups: The code groups addresses by /16 subnet, sorts descending by group size, takes the top `len/2` groups, and evicts 2 from any group with **strictly more than 4** peers (`addrs.len() > 4`). If the attacker fills the store with exactly 4 addresses per /16 subnet across 4096 subnets, every group has exactly 4 — the `> 4` condition is never satisfied, producing zero candidates.

With both paths exhausted, `check_purge` returns `Err(PeerStoreError::EvictionFailed)`, which propagates out of `add_addr`. The caller (`DiscoveryAddressManager::add_new_addrs`) silently discards the error. The peer store is permanently frozen at 16384 entries.

### Impact Explanation
The node can no longer learn about new peers via the discovery protocol. If existing connections drop (e.g., due to natural churn, bans, or targeted disconnection), the node cannot find replacements. The malicious entries persist across restarts because the peer store is saved to disk (`peer_store_db.rs`). The node becomes progressively isolated from the honest network.

### Likelihood Explanation
The attacker needs only a single P2P connection to the victim. A non-announce `Nodes` message can carry up to 1000 items; with 3 addresses per item that is 3000 addresses per message. Filling 16384 slots requires roughly 6 messages, deliverable in seconds. The attacker does not need to own the spoofed IPs — they only need to pass the `is_reachable` check, which any globally routable address satisfies. No PoW, no privileged access, no Sybil attack is required.

### Recommendation
1. Change the eviction threshold from `> 4` to `>= 4` (or `> 3`) so groups of exactly 4 are also eligible for eviction.
2. Cap the number of addresses accepted per /16 subnet before they reach the store (e.g., reject the 5th address from the same /16 at `add_addr` time).
3. Rate-limit the number of addresses accepted per session/peer to prevent rapid store saturation.
4. When `EvictionFailed` is returned, consider evicting the oldest/lowest-score entry unconditionally rather than failing silently.

### Proof of Concept
```
1. Attacker connects to victim CKB node via P2P.
2. Attacker sends ~6 discovery Nodes messages (announce=false), each containing
   ~1000 Node items with addresses from distinct /16 subnets:
     1.0.0.1:8115, 1.0.0.2:8115, 1.0.0.3:8115, 1.0.0.4:8115  (subnet 1.0/16)
     1.1.0.1:8115, 1.1.0.2:8115, 1.1.0.3:8115, 1.1.0.4:8115  (subnet 1.1/16)
     ... (4096 subnets × 4 addresses = 16384 total)
3. After all messages are processed, addr_manager.count() == 16384.
4. Any subsequent add_addr call triggers check_purge:
   - Path 1: all entries have is_connectable==true → 0 candidates
   - Path 2: 4096 groups of 4, none > 4 → 0 candidates
   - Returns EvictionFailed
5. Node silently drops all future discovered addresses.
   Peer store is frozen; node cannot discover new honest peers.
```

### Citations

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```

**File:** network/src/peer_store/mod.rs (L34-35)
```rust
const ADDR_MAX_RETRIES: u32 = 3;
const ADDR_MAX_FAILURES: u32 = 10;
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

**File:** network/src/network_group.rs (L26-29)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
            }
```
