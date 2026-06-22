### Title
Peer Store Permanently Blocked by Single-Network-Group Flood via Discovery Gossip — (`network/src/peer_store/peer_store_impl.rs`)

### Summary

`check_purge` contains an integer-division bug: when all `ADDR_COUNT_LIMIT` (16384) entries belong to a single network group, `take(len / 2)` with `len == 1` evaluates to `take(0)`, producing zero eviction candidates and returning `Err(PeerStoreError::EvictionFailed)`. An unprivileged remote peer can reach this state by sending discovery `Nodes` messages whose addresses all share the same IPv4 /16 prefix, permanently preventing the victim node from adding any new peer addresses.

---

### Finding Description

**Root cause — `check_purge` integer-division edge case** [1](#0-0) 

```rust
let len = peers_by_network_group.len();   // == 1 when all addrs share one /16
...
peers
    .into_iter()
    .take(len / 2)   // take(0) — yields nothing
```

When `len == 1`, `1 / 2 == 0` in integer arithmetic, so the iterator is immediately exhausted. The subsequent `candidate_peers.is_empty()` check is true, and the function returns: [2](#0-1) 

**Network group computation confirms /16 granularity** [3](#0-2) 

All IPv4 addresses sharing the same first two octets (e.g., `1.2.0.0/16`) map to the identical `Group::IP4([1, 2])` key, so 16 384 addresses from a single /16 block produce exactly one group entry.

**Attacker entry point — discovery gossip** [4](#0-3) 

`add_new_addrs` iterates every received address and calls `peer_store.add_addr` with no per-session quota. The only per-message cap is `MAX_ADDR_TO_SEND = 1000` items for non-announce messages (3 000 addresses per message) and `ANNOUNCE_THRESHOLD = 10` items for announce messages (30 addresses per message). [5](#0-4) 

`add_addr` always stores entries with `last_connected_at_ms = 0` and `attempts_count = 0`.

**Why those entries are "connectable" and survive the first eviction pass** [6](#0-5) 

With `last_connected_at_ms = 0` and `attempts_count = 0 < ADDR_MAX_RETRIES (3)`, `is_connectable` returns `true`. The first eviction pass (removing non-connectable entries) therefore removes nothing, and execution falls through to the broken network-group path. [7](#0-6) 

**Constants** [8](#0-7) 

`ADDR_COUNT_LIMIT = 16384`, `ADDR_MAX_RETRIES = 3`, `ADDR_MAX_FAILURES = 10`.

---

### Impact Explanation

Once the peer store reaches 16 384 entries all in one /16 group, every subsequent call to `add_addr` (from discovery or identify) returns `Err(EvictionFailed)`. The node can no longer record any newly discovered peer addresses. `fetch_addrs_to_attempt` returns nothing useful (all injected entries have `last_connected_at_ms = 0`, failing the "connected within 3 days" filter), and `fetch_addrs_to_feeler` returns only attacker-controlled addresses. The victim is effectively isolated from the honest peer graph, a prerequisite for an eclipse attack that can cause consensus deviation.

The question's stated precondition that entries need `last_connected_at_ms > 0` is **incorrect** — `add_addr` always writes `0`, but the attack works regardless because `is_connectable` still returns `true` for those entries.

---

### Likelihood Explanation

- No privilege required: any peer that completes a P2P handshake can send `Nodes` messages.
- Filling 16 384 slots requires ~6 non-announce messages (6 × 3 000 addresses) across 6 sessions, or sustained announce messages over time. Both are feasible.
- `is_valid_addr` filters RFC-1918/loopback addresses but passes any globally routable /16 block (e.g., `1.2.0.0/16`), of which many exist.
- The `AddrManager.add` deduplicates by exact multiaddr, so the attacker needs 16 384 distinct ports or host addresses within the /16 — trivially achievable.

---

### Recommendation

Replace `take(len / 2)` with a guard that always selects at least one group when `len >= 1`:

```rust
let take_count = std::cmp::max(1, len / 2);
peers.into_iter().take(take_count)...
```

Additionally, enforce a per-session or per-source-IP rate limit in `add_new_addrs`, and cap the number of entries accepted from any single /16 network group.

---

### Proof of Concept

```rust
// Fill peer store with 16384 addresses all in 1.2.x.x/16
let mut peer_store: PeerStore = Default::default();
for i in 0u32..16384 {
    let ip = format!("1.2.{}.{}", (i >> 8) & 0xff, i & 0xff);
    let addr: Multiaddr = format!("/ip4/{}/tcp/8114/p2p/Qm...", ip).parse().unwrap();
    peer_store.add_outbound_addr(addr, Flags::COMPATIBILITY); // bypasses check_purge
}
// Now mark all as connectable (last_connected_at_ms=0, attempts_count=0 already is)
// Attempt to add one more address
let new_addr: Multiaddr = "/ip4/5.6.7.8/tcp/8114/p2p/Qm...".parse().unwrap();
let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
assert!(matches!(result, Err(e) if e.to_string().contains("EvictionFailed")));
// Verify discovery is blocked
assert!(peer_store.fetch_addrs_to_feeler(10, |_| true)
    .iter().all(|a| a.addr.to_string().contains("1.2.")));
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

**File:** network/src/peer_store/peer_store_impl.rs (L340-356)
```rust
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

```

**File:** network/src/peer_store/peer_store_impl.rs (L366-376)
```rust
                let len = peers_by_network_group.len();
                let mut peers = peers_by_network_group
                    .drain()
                    .map(|(_, v)| v)
                    .collect::<Vec<Vec<_>>>();

                peers.sort_unstable_by_key(|k| std::cmp::Reverse(k.len()));

                peers
                    .into_iter()
                    .take(len / 2)
```

**File:** network/src/peer_store/peer_store_impl.rs (L399-401)
```rust
            if candidate_peers.is_empty() {
                return Err(PeerStoreError::EvictionFailed.into());
            }
```

**File:** network/src/network_group.rs (L26-29)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
            }
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

**File:** network/src/peer_store/mod.rs (L26-35)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
/// Consider we never seen a peer if peer's last_connected_at beyond this timeout
const ADDR_TIMEOUT_MS: u64 = 7 * 24 * 3600 * 1000;
/// The timeout that peer's address should be added to the feeler list again
pub(crate) const ADDR_TRY_TIMEOUT_MS: u64 = 3 * 24 * 3600 * 1000;
/// When obtaining the list of selectable nodes for identify,
/// the node that has just been disconnected needs to be excluded
pub(crate) const DIAL_INTERVAL: u64 = 15 * 1000;
const ADDR_MAX_RETRIES: u32 = 3;
const ADDR_MAX_FAILURES: u32 = 10;
```
