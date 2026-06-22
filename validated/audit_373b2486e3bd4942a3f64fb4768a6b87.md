Looking at the actual code, I need to verify each step of the claimed exploit path.

### Title
Single-Group Monopoly Causes Permanent `EvictionFailed` in `check_purge`, Blocking Peer Discovery — (`network/src/peer_store/peer_store_impl.rs`)

---

### Summary

A logic bug in `check_purge`'s network-group eviction path uses integer division `take(len / 2)` where `len` is the number of distinct network groups. When an attacker pre-fills the peer store with `ADDR_COUNT_LIMIT` (16384) connectable addresses all belonging to the same network group, `len = 1`, `1 / 2 = 0` in integer arithmetic, `take(0)` yields an empty iterator, `candidate_peers` is empty, and the function returns `Err(PeerStoreError::EvictionFailed)`. Every subsequent call to `add_addr` fails, permanently blocking the node from learning new peers via discovery.

---

### Finding Description

**Step 1 — Capacity check and first-pass eviction**

`check_purge` is called from `add_addr` before inserting a new address. [1](#0-0) 

The first pass collects addresses where `is_connectable(now_ms)` returns `false` and removes them. [2](#0-1) 

**Step 2 — Freshly added addresses are always connectable**

`AddrInfo::new` sets `last_connected_at_ms=0`, `last_tried_at_ms=0`, `attempts_count=0`. For `is_connectable`:
- `tried_in_last_minute`: `0 >= now_ms - 60000` → false (now_ms ≈ 1.7 × 10¹² ms)
- `last_connected_at_ms==0 && attempts_count >= ADDR_MAX_RETRIES(3)`: `0 >= 3` → false
- `now_ms - 0 > ADDR_TIMEOUT_MS && attempts_count >= ADDR_MAX_FAILURES(10)`: `0 >= 10` → false

All three conditions are false, so `is_connectable` returns `true`. [3](#0-2) 

The first pass evicts nothing; `candidate_peers.is_empty()` is true, and execution falls into the second pass.

**Step 3 — Network group is /16, not /24**

The `Group` enum keys on the first **two** octets of IPv4:

```rust
return Group::IP4([bits[0], bits[1]]);
``` [4](#0-3) 

An attacker using any 16384 addresses in the same /16 (e.g., `10.0.0.0–10.0.63.255`) produces exactly one group. A /16 contains 65536 addresses, so filling 16384 slots is trivially feasible.

**Step 4 — The integer-division bug**

```rust
let len = peers_by_network_group.len();   // = 1
...
peers.into_iter().take(len / 2)           // take(0) — empty!
``` [5](#0-4) 

`take(0)` produces an empty iterator regardless of the `addrs.len() > 4` guard inside `flat_map`. `candidate_peers` is empty.

**Step 5 — Permanent failure**

```rust
if candidate_peers.is_empty() {
    return Err(PeerStoreError::EvictionFailed.into());
}
``` [6](#0-5) 

`add_addr` propagates this error via `self.check_purge()?`, so every subsequent discovery-sourced address is rejected. [7](#0-6) 

---

### Impact Explanation

Once the peer store is saturated with single-group addresses, the node can no longer accept any new peer addresses from the discovery protocol. Peer discovery is permanently blocked for the lifetime of the process (or until the store is manually cleared). The node cannot find new outbound connections, degrading its ability to sync and relay transactions/blocks. This matches the stated **Low (501–2000)** scope.

---

### Likelihood Explanation

The discovery protocol is an unauthenticated P2P path. An attacker controlling a single node can send `GetNodes`/`Nodes` messages advertising 16384 addresses in the same /16 subnet. No PoW, no key material, and no privileged access is required. The addresses do not need to be reachable — they only need to be syntactically valid multiaddrs. The attack is deterministic and locally reproducible.

---

### Recommendation

Replace `take(len / 2)` with a minimum-of-one guard:

```rust
peers.into_iter().take((len / 2).max(1))
```

This ensures at least the largest group is always a candidate for eviction when the store is full. Additionally, consider capping the number of addresses accepted per network group during `add_addr` to prevent monopoly pre-filling.

---

### Proof of Concept

```rust
// Fill addr_manager with ADDR_COUNT_LIMIT addresses from the same /16 (10.0.x.x)
let mut peer_store = PeerStore::default();
for i in 0u32..16384 {
    let ip = std::net::Ipv4Addr::new(10, 0, (i / 256) as u8, (i % 256) as u8);
    let addr: Multiaddr = format!("/ip4/{}/tcp/8115", ip).parse().unwrap();
    // First 16383 succeed; store reaches ADDR_COUNT_LIMIT
    let _ = peer_store.add_addr(addr, Flags::COMPATIBILITY);
}
// Now add one legitimate address from a different /16
let new_addr: Multiaddr = "/ip4/1.2.3.4/tcp/8115".parse().unwrap();
let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
// Expected (fixed): Ok(())
// Actual (buggy):   Err(EvictionFailed)
assert!(result.is_ok(), "peer discovery permanently blocked");
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
