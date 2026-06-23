### Title
Peer Store Eviction Deadlock via Connectable Address Flooding — (`network/src/peer_store/peer_store_impl.rs`)

### Summary

`check_purge` has two sequential eviction passes. An attacker who fills `addr_manager` with exactly `ADDR_COUNT_LIMIT` (16 384) addresses that are all connectable **and** distributed so no network group exceeds 4 entries can cause both passes to produce zero candidates, returning `PeerStoreError::EvictionFailed` and making every subsequent `add_addr` call fail.

---

### Finding Description

**Pass 1 — non-connectable eviction** [1](#0-0) 

Addresses added via `add_addr` are created with `last_connected_at_ms = 0` and `attempts_count = 0`. [2](#0-1) 

With `attempts_count = 0 < ADDR_MAX_RETRIES (3)` and `attempts_count = 0 < ADDR_MAX_FAILURES (10)`, `is_connectable` returns `true` for every freshly injected address. Pass 1 collects zero candidates.

**Pass 2 — network-group eviction** [3](#0-2) 

Pass 2 only evicts from groups where `addrs.len() > 4`. If the attacker distributes 16 384 addresses across ≥ 4 097 distinct network groups with ≤ 4 addresses each, every group fails the `> 4` threshold. `candidate_peers` is empty and the function returns `Err(PeerStoreError::EvictionFailed)`.

**Effect on `add_addr`** [4](#0-3) 

`check_purge()?` propagates the error, so every Discovery-sourced `add_addr` call fails while the store remains in this state.

**Sustaining the state**

`AddrManager::add` re-inserts an existing address when `new.last_connected_at_ms >= existing.last_connected_at_ms`. [5](#0-4) 

Since `add_addr` always passes `last_connected_at_ms = 0`, re-advertising the same address resets `attempts_count` to 0, keeping it connectable and preventing natural eviction.

---

### Impact Explanation

While the node retains its 16 384 stored addresses and existing connections are unaffected, it cannot absorb any new peer advertisements from honest Discovery peers. This degrades the node's ability to recover from churn, find peers after restarts, or grow its view of the network. The effect persists as long as the attacker re-advertises the flooding addresses to reset their `attempts_count`.

---

### Likelihood Explanation

The Discovery protocol imposes no per-peer cap on how many addresses can be relayed over time. A single malicious peer maintaining one connection can drip-feed 16 384 distinct addresses (spread across many /24 or finer subnets) over multiple Discovery rounds. No PoW, stake, or privileged role is required. [6](#0-5) 

`ADDR_COUNT_LIMIT = 16 384` and `ADDR_TRY_TIMEOUT_MS = 3 days` are the only relevant thresholds, and neither is enforced per-source.

---

### Recommendation

1. **Enforce a per-source-IP cap** inside `add_addr` so a single peer cannot contribute more than a small fraction of `ADDR_COUNT_LIMIT`.
2. **Relax the eviction threshold** in pass 2: evict from the largest group even if it has ≤ 4 entries (e.g., always evict from the top group regardless of size) so the store can never be permanently locked.
3. **Track injection source** and rate-limit Discovery `GetNodes`/`Nodes` message processing.

---

### Proof of Concept

```rust
// Pseudocode – reproducible in a unit test
let mut store = PeerStore::default();
// Fill with ADDR_COUNT_LIMIT addresses: 4096 distinct /24 groups × 4 addresses each
for group in 0u32..4096 {
    for host in 1u32..=4 {
        let ip = format!("10.{}.{}.{}:8115", group / 256, group % 256, host);
        let addr = multiaddr_from_ip(&ip);
        store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
}
// Store is now at ADDR_COUNT_LIMIT; all entries connectable, no group > 4
let new_addr = multiaddr_from_ip("1.2.3.4:8115");
let result = store.add_addr(new_addr, Flags::COMPATIBILITY);
assert!(matches!(result, Err(e) if e.to_string().contains("EvictionFailed")));
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

**File:** network/src/peer_store/peer_store_impl.rs (L357-401)
```rust
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

**File:** network/src/peer_store/mod.rs (L26-30)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
/// Consider we never seen a peer if peer's last_connected_at beyond this timeout
const ADDR_TIMEOUT_MS: u64 = 7 * 24 * 3600 * 1000;
/// The timeout that peer's address should be added to the feeler list again
pub(crate) const ADDR_TRY_TIMEOUT_MS: u64 = 3 * 24 * 3600 * 1000;
```
