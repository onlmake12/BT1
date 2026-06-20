### Title
Peer Store Permanently Blocked by Unevictable Addresses from Distinct /16 Subnets — (`network/src/peer_store/peer_store_impl.rs`, `network/src/peer_store/types.rs`)

---

### Summary

The `check_purge` eviction logic in `PeerStore` has two sequential strategies, both of which fail when the store is filled with 16384 entries having `last_connected_at_ms=0` and `attempts_count=0` from distinct /16 subnets. Once full, every subsequent `add_addr` call returns `Err(EvictionFailed)`, permanently blocking new peer address ingestion.

---

### Finding Description

**Constants:**
- `ADDR_COUNT_LIMIT = 16384` [1](#0-0) 
- `ADDR_MAX_RETRIES = 3`, `ADDR_MAX_FAILURES = 10` [2](#0-1) 

**`add_addr` always sets `last_connected_at_ms=0`:**

All addresses ingested via discovery start with `last_connected_at_ms=0` and `attempts_count=0` by construction: [3](#0-2) 

**`is_connectable` returns `true` for all such entries:**

For an entry with `last_connected_at_ms=0, attempts_count=0, last_tried_at_ms=0`:
- `tried_in_last_minute`: `0 >= now_ms - 60000` → false (node running >1 min)
- `last_connected_at_ms == 0 && attempts_count >= 3`: `0 >= 3` → false
- `now_ms - 0 > ADDR_TIMEOUT_MS && attempts_count >= 10`: `0 >= 10` → false
- Returns **true** [4](#0-3) 

**`check_purge` Strategy 1 — evict non-connectable entries — finds nothing:**

```
candidate_peers = addrs where !is_connectable(now_ms)
```
All 16384 entries pass `is_connectable`, so `candidate_peers` is empty. [5](#0-4) 

**`check_purge` Strategy 2 — network group eviction — also finds nothing:**

The network group is `Group::IP4([bits[0], bits[1]])` — the first two octets of IPv4 (i.e., the /16 prefix). [6](#0-5) 

With 16384 entries each from a distinct /16 subnet, every group has exactly 1 peer. The eviction only removes from groups with `> 4` peers: [7](#0-6) 

Since `1 > 4` is false for every group, `candidate_peers` is again empty.

**Result: `Err(EvictionFailed)` returned permanently:** [8](#0-7) 

This propagates through `add_addr` via `?`: [9](#0-8) 

**Attack entry point — P2P discovery protocol:**

`add_addr` is called from `network/src/protocols/discovery/mod.rs` when processing `Nodes` messages from remote peers. An unprivileged remote peer (or a set of Sybil peers) can advertise addresses from 16384 distinct /16 subnets over time to fill the store. [10](#0-9) 

---

### Impact Explanation

Once the store is saturated, the node can no longer ingest any new peer addresses via discovery, identify, or DNS seeding. The node is starved of fresh peers, degrading its ability to find new outbound connections. This is a targeted peer-discovery denial-of-service. The condition persists until the node is restarted and the stored addresses age out or are attempted (incrementing `attempts_count` toward `ADDR_MAX_RETRIES=3`), but an attacker can re-fill the store immediately after restart.

---

### Likelihood Explanation

The attack requires advertising 16384 addresses from distinct /16 subnets. IPv4 has 65536 possible /16 prefixes, so the address space is sufficient. The attacker can use multiple Sybil peers, each advertising a batch of addresses. There is no per-peer rate limit visible in the peer store logic that would prevent accumulation over time. The attack is low-cost and requires no special privileges.

---

### Recommendation

1. **Fix `is_connectable` for never-tried entries**: Entries with `last_connected_at_ms=0` and `attempts_count=0` that have been in the store beyond a threshold (e.g., `ADDR_TIMEOUT_MS`) should be considered evictable, not connectable.
2. **Fix Strategy 2 threshold**: Lower the group-size threshold from `> 4` to `>= 1` (or `>= 2`) so that even single-peer groups can be evicted when the store is full.
3. **Cap per-/16 subnet entries at ingestion time**: Reject `add_addr` calls for subnets already at a per-group cap (e.g., 4 entries per /16) before they reach the store.

---

### Proof of Concept

```rust
// Pseudocode unit test
let mut peer_store = PeerStore::default();
for i in 0u32..16384 {
    // Each address from a distinct /16: 0.0.x.1, 1.0.x.1, ... across 16384 /16s
    let a = (i >> 8) as u8;
    let b = (i & 0xff) as u8;
    let addr: Multiaddr = format!("/ip4/{}.{}.0.1/tcp/8115", a, b).parse().unwrap();
    // add_addr sets last_connected_at_ms=0, attempts_count=0 internally
    peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
}
// Store is now at ADDR_COUNT_LIMIT=16384, all entries unevictable
let new_addr: Multiaddr = "/ip4/200.200.0.1/tcp/8115".parse().unwrap();
let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
assert!(matches!(result, Err(e) if e.to_string().contains("EvictionFailed")));
// Store count unchanged at 16384
assert_eq!(peer_store.addr_manager().count(), 16384);
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

**File:** network/src/peer_store/peer_store_impl.rs (L378-390)
```rust
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

**File:** network/src/network_group.rs (L26-29)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
            }
```

**File:** network/src/protocols/discovery/mod.rs (L1-1)
```rust
use std::{collections::HashMap, sync::Arc};
```
