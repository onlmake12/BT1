The code path is real. Let me trace it precisely.

### Title
Sustained O(n) CPU Work Per `add_addr` Call When Peer Store Is Saturated With Connectable Diverse-Subnet Entries — (`network/src/peer_store/peer_store_impl.rs`)

---

### Summary

When `AddrManager` is at `ADDR_COUNT_LIMIT` (16 384) and every stored entry is connectable and spread across distinct network groups (no group exceeds 4 peers), `check_purge` performs a full O(n) scan plus an O(k log k) sort on **every** `add_addr` call, returns `Err(EvictionFailed)`, and leaves the count unchanged at 16 384. Because the new address is never inserted, the count never drops below the limit, so the next call repeats the same work. This is not amortised — it is O(n) per call, sustained, and reachable from any unprivileged P2P peer via the discovery `Nodes` message.

---

### Finding Description

**Entry point — discovery `Nodes` message:**

`DiscoveryAddressManager::add_new_addrs` iterates over every address in a received `Nodes` message and calls `peer_store.add_addr()` for each one. Errors are silently swallowed at `debug!` level; the loop continues unconditionally. [1](#0-0) 

A single non-announce `Nodes` message may carry up to `MAX_ADDR_TO_SEND = 1000` nodes × `MAX_ADDRS = 3` addresses = **3 000 `add_addr` calls per message**. [2](#0-1) 

**`add_addr` always calls `check_purge` before inserting:** [3](#0-2) 

**`check_purge` logic when the store is full:** [4](#0-3) 

When `count >= ADDR_COUNT_LIMIT`, the function does **two sequential full scans**:

1. **Phase 1** — full `addrs_iter()` scan to collect non-connectable entries (O(n)): [5](#0-4) 

2. **Phase 2** (entered only when Phase 1 finds nothing) — second full `addrs_iter()` scan, `HashMap<Group, Vec<_>>` construction, and `sort_unstable_by_key` (O(n) + O(k log k)): [6](#0-5) 

3. If no network group has more than 4 peers, `candidate_peers` is still empty and the function returns `Err(PeerStoreError::EvictionFailed)`: [7](#0-6) 

**Why the count never drops:** `check_purge` returns `Err` before `addr_manager.add()` is reached, so the new address is never inserted. The store stays at exactly 16 384 entries. Every subsequent `add_addr` call re-enters the same two-phase scan.

**Why entries stay connectable by default:** A freshly added `AddrInfo` has `last_connected_at_ms = 0` and `attempts_count = 0`. `is_connectable` returns `true` because `attempts_count (0) < ADDR_MAX_RETRIES (3)`: [8](#0-7) 

`ADDR_COUNT_LIMIT` is 16 384: [9](#0-8) 

---

### Impact Explanation

With n = 16 384 and k up to 16 384 distinct `/16` groups, each `add_addr` call performs roughly 32 768 HashMap insertions/lookups plus a sort of up to 16 384 buckets. A single `Nodes` message with 1 000 nodes × 3 addresses triggers ~3 000 such calls, totalling on the order of **~100 M operations per message**. This is bounded but sustained: the CPU cost is proportional to n on every message as long as the precondition holds. The node does not crash, but its network-service thread is monopolised processing peer-store work instead of useful protocol logic.

---

### Likelihood Explanation

The precondition is reachable by an unprivileged attacker:

- **Fill the store**: send 16 384 unique addresses (e.g., IPv6 or spoofed IPv4) spread across ≥ 4 097 distinct `/16` subnets so no group exceeds 4. Each entry is connectable by default (attempts_count = 0).
- **Sustain the load**: keep sending `Nodes` messages with new addresses. Each message triggers 3 000 × O(n) calls. The error is logged at `debug!` only; no disconnect or rate-limit is applied to the sender.

The only practical mitigations are the node's inbound connection limit and the OS TCP stack, neither of which limits the rate of `Nodes` messages on an established session.

---

### Recommendation

1. **Cache the eviction-failed state**: after `check_purge` returns `Err(EvictionFailed)`, set a flag (e.g., `store_full_no_eviction: bool`) and return `Err` immediately on subsequent calls without re-scanning, until an entry is removed or expires.
2. **Alternatively, cap Phase 2 work**: if Phase 1 finds no candidates, skip Phase 2 entirely and return `Err` immediately. The sort provides no benefit when the result is always `Err`.
3. **Rate-limit `add_addr` calls per session**: track how many addresses a session has contributed and throttle or drop excess advertisements.

---

### Proof of Concept

```rust
// Fill AddrManager to ADDR_COUNT_LIMIT with connectable, diverse-subnet entries
let mut peer_store = PeerStore::default();
for i in 0u32..16384 {
    // Each address in a unique /16 subnet; attempts_count=0 → connectable
    let addr: Multiaddr = format!("/ip4/{}.{}.0.1/tcp/8115", i >> 8, i & 0xff)
        .parse().unwrap();
    peer_store.add_outbound_addr(addr, Flags::COMPATIBILITY);
}
assert_eq!(peer_store.addr_manager().count(), 16384);

// Now measure CPU time per add_addr call
let new_addr: Multiaddr = "/ip4/200.200.200.1/tcp/8115".parse().unwrap();
let t0 = std::time::Instant::now();
for _ in 0..1000 {
    let _ = peer_store.add_addr(new_addr.clone(), Flags::COMPATIBILITY);
    // Each call: O(n) scan + O(k log k) sort + Err; count stays 16384
}
let elapsed = t0.elapsed();
// Assert superlinear growth vs. a store with count < ADDR_COUNT_LIMIT
println!("1000 saturated add_addr calls: {:?}", elapsed);
// Expected: orders of magnitude slower than 1000 calls on a non-full store
```

### Citations

**File:** network/src/protocols/discovery/mod.rs (L32-34)
```rust
const MAX_ADDR_TO_SEND: usize = 1000;
// The maximum number addresses in one Nodes item
const MAX_ADDRS: usize = 3;
```

**File:** network/src/protocols/discovery/mod.rs (L352-362)
```rust
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

**File:** network/src/peer_store/peer_store_impl.rs (L327-330)
```rust
    fn check_purge(&mut self) -> Result<()> {
        if self.addr_manager.count() < ADDR_COUNT_LIMIT {
            return Ok(());
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
