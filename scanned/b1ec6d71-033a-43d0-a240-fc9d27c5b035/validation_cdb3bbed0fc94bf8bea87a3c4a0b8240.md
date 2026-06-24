The code is confirmed exactly as cited. Let me verify the mathematical analysis before rendering judgment.

**Confirmed code at lines 366–400 of `peer_store_impl.rs`:**

- `let len = peers_by_network_group.len();` [1](#0-0) 
- `.take(len / 2)` — integer (floor) division [2](#0-1) 
- `if addrs.len() > 4` — strict greater-than, not `>=` [3](#0-2) 
- `return Err(PeerStoreError::EvictionFailed.into())` when `candidate_peers` is empty [4](#0-3) 

`ADDR_COUNT_LIMIT = 16384`. With 4096 groups of exactly 4 peers each (4096 × 4 = 16384), `len = 4096` (even), `take(2048)` considers 2048 groups — but every group has `len == 4`, so `4 > 4` is always `false`, `flat_map` returns `None` for every group, `candidate_peers` is empty, and `EvictionFailed` is returned. The `> 4` threshold is the primary flaw; the floor-division issue compounds it for odd group counts. [5](#0-4) 

`add_addr` propagates this error via `?` at line 75. [6](#0-5) 

Addresses with `last_connected_at_ms = 0` and `attempts_count = 0` pass `is_connectable` (they only fail when `attempts_count >= ADDR_MAX_RETRIES = 3`), so freshly injected addresses bypass the first eviction pass entirely. [7](#0-6) 

The flaw is real, the exploit path is reachable by an unprivileged P2P participant, and the impact (permanent peer store lockout → node isolation) maps to the allowed High impact class. The finding is valid.

---

Audit Report

## Title
Peer Store Permanently Blocked via Crafted Group-of-4 Address Distribution — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
`check_purge` contains two compounding logic errors: `take(len / 2)` (floor division) and `addrs.len() > 4` (strict threshold). When an attacker fills the peer store with exactly 4 connectable addresses per `/16` network group, every group fails the `> 4` check, `candidate_peers` is always empty, and `check_purge` permanently returns `EvictionFailed`. Because `add_addr` propagates this error, the node can no longer accept any new peer addresses, leading to eventual network isolation.

## Finding Description
In `check_purge` (lines 327–404 of `peer_store_impl.rs`), after the first pass removes non-connectable peers, the group-based eviction path executes:

```rust
let len = peers_by_network_group.len();          // e.g. 4096
peers.sort_unstable_by_key(|k| Reverse(k.len()));
peers
    .into_iter()
    .take(len / 2)                               // take(2048)
    .flat_map(move |addrs| {
        if addrs.len() > 4 {                     // 4 > 4 == false
            Some(...)
        } else {
            None                                 // always None
        }
    })
    .flatten()
    .collect()                                   // always empty
```

With `ADDR_COUNT_LIMIT = 16384` and 4096 groups of exactly 4 peers each (4096 × 4 = 16384), `len = 4096`, `take(2048)` considers 2048 groups, but every group has `len == 4`, so `4 > 4` is `false` for all of them. `candidate_peers` is empty, and the function hits `return Err(PeerStoreError::EvictionFailed.into())`. For odd group counts (e.g., `len = 3`), `take(1)` considers only one group, making the dead zone even easier to trigger. `add_addr` propagates the error via `self.check_purge()?` at line 75, permanently blocking all new address insertions.

Freshly injected addresses with `last_connected_at_ms = 0` and `attempts_count = 0` are considered connectable by `is_connectable` (they only fail when `attempts_count >= ADDR_MAX_RETRIES = 3`), so they survive the first eviction pass and feed directly into the dead zone.

## Impact Explanation
Once the dead zone is triggered, `add_addr` returns `Err` for every subsequent call. The node's peer store is permanently locked: it cannot learn about new peers via the discovery protocol. If any existing connections drop, the node has no mechanism to find replacements and becomes isolated from the CKB network. This maps to **High: Vulnerabilities which could easily crash a CKB node** (10001–15000 points), as a fully isolated node is effectively non-functional as a network participant.

## Likelihood Explanation
An attacker needs only a single P2P connection to the victim node to advertise addresses via the discovery protocol. IPv4 has 65536 possible `/16` blocks; filling 4096 of them with exactly 4 addresses each (16384 total) is straightforward. All injected addresses need `last_connected_at_ms = 0` and `attempts_count = 0`, which is the default for newly advertised addresses (`AddrInfo::new` sets both to 0). No special privileges, no victim mistakes, and no external dependencies are required. The attack is repeatable and deterministic.

## Recommendation
1. Replace `addrs.len() > 4` with `addrs.len() >= 4` (or equivalently `> 3`) so that groups of exactly 4 are eligible for eviction.
2. Replace `take(len / 2)` with `take((len + 1) / 2)` (ceiling division) to ensure at least one group is always considered when `len` is odd.
3. Add a fallback: if `candidate_peers` is still empty after the group pass, unconditionally evict one address from the largest group, preventing `EvictionFailed` when the store is full of connectable peers.

## Proof of Concept
```rust
// In a test or fuzz harness:
let mut peer_store = PeerStore::default();
// Fill with ceil(ADDR_COUNT_LIMIT / 4) = 4096 groups,
// each with exactly 4 peers from distinct /16 blocks:
// 1.1.0.1, 1.1.0.2, 1.1.0.3, 1.1.0.4  (group 1.1.x.x)
// 2.2.0.1, 2.2.0.2, 2.2.0.3, 2.2.0.4  (group 2.2.x.x)
// ... up to 4096 groups ...
// All peers: last_connected_at_ms=0, attempts_count=0 (connectable)
for g in 0u16..4096 {
    let a = (g >> 8) as u8 + 1;
    let b = (g & 0xff) as u8 + 1;
    for h in 1u8..=4 {
        let addr = format!("/ip4/{}.{}.0.{}/tcp/8115", a, b, h).parse().unwrap();
        peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
}
// Store is now at ADDR_COUNT_LIMIT with 4096 groups of 4 connectable peers each.
// Any further add_addr call returns Err(EvictionFailed):
let new_addr = "/ip4/5.5.0.1/tcp/8115".parse().unwrap();
assert!(peer_store.add_addr(new_addr, Flags::COMPATIBILITY).is_err());
```

### Citations

**File:** network/src/peer_store/peer_store_impl.rs (L75-75)
```rust
        self.check_purge()?;
```

**File:** network/src/peer_store/peer_store_impl.rs (L366-366)
```rust
                let len = peers_by_network_group.len();
```

**File:** network/src/peer_store/peer_store_impl.rs (L374-392)
```rust
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
