Audit Report

## Title
Peer Store Permanent DoS via Single-Group Address Flooding — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
`check_purge`'s network-group fallback computes `take(len / 2)` where `len` is the number of distinct network groups. When all 16,384 stored addresses belong to a single `/16` subnet, `len = 1` and `1 / 2 = 0` in integer division, so `take(0)` produces an empty iterator, no addresses are evicted, and `Err(PeerStoreError::EvictionFailed)` is returned. Because `add_addr` propagates this error via `?`, the peer store permanently rejects every subsequent address insertion — from any network group — until the node is restarted.

## Finding Description

**Root cause — `check_purge` integer division:** [1](#0-0) 

When `peers_by_network_group.len() == 1`, `len / 2 == 0`, so `.take(0)` yields an empty iterator. `candidate_peers` is always empty, and the function unconditionally returns the error: [2](#0-1) 

**Why the first eviction step also fails:**

Freshly injected addresses have `last_connected_at_ms = 0`, `attempts_count = 0`, `last_tried_at_ms = 0`. Walking `is_connectable`: [3](#0-2) 

- `tried_in_last_minute`: `0 >= now_ms − 60_000` → false (now_ms ≈ 1.7 × 10¹²)
- `last_connected_at_ms == 0 && attempts_count >= 3`: `0 >= 3` → false
- `now_ms − 0 > ADDR_TIMEOUT_MS && attempts_count >= 10`: `0 >= 10` → false

Every freshly injected address is "connectable"; the first eviction pass removes nothing.

**Network-group key — single group for entire /16:** [4](#0-3) 

All addresses in `1.2.0.0/16` map to `Group::IP4([1, 2])`, so `peers_by_network_group.len() == 1`.

**Error propagation to caller:** [5](#0-4) 

`self.check_purge()?` propagates `EvictionFailed` to every caller of `add_addr`, permanently blocking all address insertion.

**Global limit:** [6](#0-5) 

A /16 subnet contains 65,536 addresses; filling 16,384 slots is trivially feasible.

## Impact Explanation

Once the store is locked, the node cannot record any new peer address from any network group. As existing connections drop naturally, the node cannot replenish its peer set and becomes progressively isolated from the CKB network. This constitutes an effective node crash from a network-participation standpoint, matching the **High** impact class: *Vulnerabilities which could easily crash a CKB node* (10,001–15,000 points).

## Likelihood Explanation

The attack requires only that an adversary deliver ~16,384 discovery `SendAddr` messages carrying addresses from a single /16 subnet. This is achievable from a single controlled node with no PoW, no key material, and no privileged access. The bug is deterministic: the same input always produces the same locked state. After a node restart the attacker can immediately repeat the flood.

## Recommendation

Replace the integer division with a minimum-of-one guard in `check_purge`:

```rust
// network/src/peer_store/peer_store_impl.rs, line 376
let take_count = (len / 2).max(1);
peers.into_iter().take(take_count) …
```

Additionally, add a per-group cap in `add_addr` or `AddrManager::add` to prevent any single /16 from monopolizing the store:

```rust
const MAX_ADDRS_PER_GROUP: usize = 256;
// reject insertion if this group already has >= MAX_ADDRS_PER_GROUP entries
```

## Proof of Concept

```rust
let mut peer_store = PeerStore::default();
// Fill store with 16384 addresses from a single /16
for i in 0u16..=255 {
    for j in 0u16..=63 {
        let addr: Multiaddr = format!("/ip4/1.2.{i}.{j}/tcp/8115").parse().unwrap();
        peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
}
// Store is now at ADDR_COUNT_LIMIT with exactly 1 network group.
// Any subsequent add_addr — even from a completely different /16 — must fail.
let new_addr: Multiaddr = "/ip4/9.9.9.9/tcp/8115".parse().unwrap();
assert!(peer_store.add_addr(new_addr, Flags::COMPATIBILITY).is_err());
// Assertion passes: check_purge computes len=1, len/2=0, evicts nothing,
// returns Err(EvictionFailed).
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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```
