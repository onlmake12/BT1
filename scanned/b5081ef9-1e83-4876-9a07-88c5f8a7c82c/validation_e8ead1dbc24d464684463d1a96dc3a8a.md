## Vulnerability Assessment

The integer division bug is **real and confirmed** in the code. Let me trace through the exact mechanics.

---

### Network Group Key: /16, Not /8

The question claims addresses from `225.0.0.0/8` map to one group, but the actual grouping uses the **first two octets**: [1](#0-0) 

```rust
if let IpAddr::V4(ipv4) = ip_addr {
    let bits = ipv4.octets();
    return Group::IP4([bits[0], bits[1]]);
}
```

So the attacker needs addresses from the same **/16** (e.g., all `225.0.x.x`) to produce a single group key. A /16 has 65536 addresses, more than enough to fill the store.

---

### ADDR_COUNT_LIMIT = 16384 [2](#0-1) 

The store triggers `check_purge` exactly when count reaches 16384.

---

### Freshly Added Addresses Are Always Connectable

`add_addr` inserts with `last_connected_at_ms = 0` and `attempts_count = 0`: [3](#0-2) 

`is_connectable` returns `true` for these because `attempts_count (0) < ADDR_MAX_RETRIES (3)`: [4](#0-3) 

So phase-1 of `check_purge` finds **zero** non-connectable candidates.

---

### The Integer Division Bug [5](#0-4) 

```rust
let len = peers_by_network_group.len();  // == 1 when all addrs share one /16
// ...
peers
    .into_iter()
    .take(len / 2)   // take(1/2) == take(0) — integer division truncates to zero
```

With `len == 1`: `take(0)` yields nothing. The `flat_map` produces no candidates. `candidate_peers` is empty: [6](#0-5) 

```rust
if candidate_peers.is_empty() {
    return Err(PeerStoreError::EvictionFailed.into());
}
```

`add_addr` propagates this error via `self.check_purge()?`: [7](#0-6) 

---

### Attacker Entry Point

The discovery protocol calls `add_addr` with remote-peer-advertised addresses: [8](#0-7) 

A single malicious peer can advertise 16384 distinct addresses from `225.0.x.x` (or any /16). There is no per-group cap enforced during the filling phase — addresses are accepted freely until `ADDR_COUNT_LIMIT` is reached.

---

### Verdict

**The vulnerability is real.** The exploit path is:

1. Attacker connects to victim via normal P2P
2. Sends discovery messages advertising 16384 distinct addresses from the same /16 (e.g., `225.0.0.1`–`225.0.255.255` with varying ports)
3. Victim's peer store fills to capacity with a single network group
4. Every subsequent `add_addr` call hits `check_purge` → phase-1 empty (all connectable) → phase-2 `take(0)` → `EvictionFailed`
5. No legitimate peer addresses can ever be added again

The store is permanently locked with attacker-controlled (unreachable) addresses, blocking peer discovery and effectively isolating the node.

---

### Title
Integer Division by 2 in `check_purge` Phase-2 Yields Zero Eviction Candidates When All Peers Share One Network Group, Permanently Blocking `add_addr` — (`network/src/peer_store/peer_store_impl.rs`)

### Summary
When all 16384 peer store entries belong to a single `/16` network group, `peers_by_network_group.len()` equals 1, and `take(len / 2)` = `take(0)` due to integer truncation, producing no eviction candidates. Every subsequent `add_addr` call returns `Err(EvictionFailed)`, permanently preventing new peer addresses from being stored.

### Finding Description
In `check_purge` phase-2 (`peer_store_impl.rs` line 376), the code takes the top `len / 2` network groups for eviction candidates. When `len == 1` (all stored addresses share one group key), integer division yields 0, so `take(0)` produces an empty iterator. The `flat_map` over the `> 4` size check never fires. `candidate_peers` is empty, and `Err(PeerStoreError::EvictionFailed)` is returned. This propagates through `add_addr`'s `check_purge()?` call, making every future `add_addr` fail permanently.

The group key for IPv4 uses only the first two octets (`Group::IP4([bits[0], bits[1]])`), so all addresses from any `/16` subnet (e.g., `225.0.0.0/16`) share one key. A `/16` provides 65536 distinct IPs — more than enough to fill the 16384-entry store.

Freshly added addresses have `last_connected_at_ms = 0` and `attempts_count = 0`. Since `0 < ADDR_MAX_RETRIES (3)`, `is_connectable` returns `true` for all of them, so phase-1 also finds zero candidates.

### Impact Explanation
The peer store is permanently locked. No new peer addresses can be added via `add_addr` (called from discovery, identify, and DNS seeding). The node's store contains only attacker-controlled (unreachable) addresses. Outbound connection attempts and peer discovery are effectively blocked, isolating the node from the honest network.

### Likelihood Explanation
Any peer that can connect to the victim can trigger this by advertising 16384 addresses from the same `/16` via the discovery protocol. No special privileges, PoW, or key material are required. The attack is cheap, deterministic, and permanent until the node is restarted with a cleared peer store.

### Recommendation
Replace `take(len / 2)` with `take((len / 2).max(1))` to ensure at least one group is always considered for eviction when the store is full. Additionally, enforce a per-group cap during `add_addr` to prevent a single `/16` from monopolizing the store.

### Proof of Concept
```rust
// Fill store with 16384 addresses from 225.0.x.x (same /16 → same Group key)
for i in 0u32..16384 {
    let ip = Ipv4Addr::new(225, 0, (i / 256) as u8, (i % 256) as u8);
    let addr = format!("/ip4/{}/tcp/8115/p2p/Qm...", ip).parse().unwrap();
    peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
}
// Now the store is full, all in Group::IP4([225, 0])
// Any new add_addr must fail:
let new_addr = "/ip4/1.2.3.4/tcp/8115/p2p/Qm...".parse().unwrap();
assert!(matches!(
    peer_store.add_addr(new_addr, Flags::COMPATIBILITY),
    Err(e) if e.to_string().contains("EvictionFailed")
));
// Confirm: peers_by_network_group.len() == 1, take(1/2) == take(0)
```

### Citations

**File:** network/src/network_group.rs (L26-29)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
            }
```

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
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

**File:** network/src/peer_store/types.rs (L89-97)
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
```

**File:** network/src/protocols/discovery/mod.rs (L1-1)
```rust
use std::{collections::HashMap, sync::Arc};
```
