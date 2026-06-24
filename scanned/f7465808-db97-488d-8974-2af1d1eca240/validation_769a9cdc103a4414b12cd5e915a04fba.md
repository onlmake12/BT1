Audit Report

## Title
Peer Store Permanently Blocked via Crafted Group-of-4 Address Distribution — (`network/src/peer_store/peer_store_impl.rs`)

## Summary

`check_purge` contains a logical dead zone: when all network groups contain exactly 4 peers, the combination of `take(len / 2)` (integer division) and the strict `addrs.len() > 4` threshold ensures zero eviction candidates are ever produced. `add_addr` propagates the resulting `EvictionFailed` error, permanently blocking new peer address insertion and leaving the node unable to discover peers.

## Finding Description

`ADDR_COUNT_LIMIT` is 16384. [1](#0-0)  An attacker fills the store with exactly 4096 groups of 4 peers each (4096 × 4 = 16384). When `check_purge` enters the group-based eviction path:

```
let len = peers_by_network_group.len();   // 4096
peers.sort_unstable_by_key(|k| Reverse(k.len()));
peers
    .into_iter()
    .take(len / 2)          // take(2048) — top half by group size
    .flat_map(|addrs| {
        if addrs.len() > 4  // 4 > 4 == false for every group
        { Some(...) } else { None }
    })
    .flatten()
    .collect()              // always empty
``` [2](#0-1) 

All 2048 considered groups have exactly 4 peers; none satisfy `> 4`. `candidate_peers` is empty, so the function reaches:

```rust
if candidate_peers.is_empty() {
    return Err(PeerStoreError::EvictionFailed.into());
}
``` [3](#0-2) 

`add_addr` propagates this error unconditionally via `?`:

```rust
self.check_purge()?;
``` [4](#0-3) 

The first eviction pass (non-connectable peers) is bypassed because injected addresses with `last_connected_at_ms = 0` and `attempts_count = 0` satisfy `is_connectable` — `0 < ADDR_MAX_RETRIES (3)` means the "never connected" branch does not fire. [5](#0-4) 

The odd-`len` scenario described in the report is a subset of the same flaw: any configuration where all groups have ≤ 4 peers triggers it, regardless of parity.

## Impact Explanation

Once the store is locked, every call to `add_addr` returns `Err(EvictionFailed)`. The node cannot record newly discovered peer addresses. As existing connections age out or disconnect, the node has no pool from which to establish new outbound connections, leading to complete network isolation. This matches **High** impact: *Vulnerabilities which could easily crash a CKB node* (10001–15000 points).

## Likelihood Explanation

Any unprivileged P2P participant can advertise arbitrary addresses via the CKB discovery protocol. The attacker needs to inject 16384 addresses spread across 4096 distinct `/16` IPv4 blocks (e.g., `1.1.x.x` through `4096.x.x.x` equivalents), each with `last_connected_at_ms = 0` and `attempts_count = 0`. These are the default values for newly added addresses. [6](#0-5)  No special privileges, victim mistakes, or external dependencies are required. The attack is repeatable: if the store is ever purged by other means, the attacker re-floods it.

## Recommendation

Two independent fixes are needed:

1. Replace `addrs.len() > 4` with `addrs.len() >= 4` (or `> 3`) so groups of exactly 4 qualify for eviction. [7](#0-6) 
2. Replace `take(len / 2)` with `take((len + 1) / 2)` (ceiling division) to ensure at least one group is always considered when `len` is odd. [8](#0-7) 
3. Add a fallback: if `candidate_peers` is still empty after the group pass, unconditionally evict one address from the largest group to guarantee forward progress.

## Proof of Concept

```rust
// Using the existing `add_addr_fuzz` interface (feature = "fuzz"):
let mut store = PeerStore::default();
// Fill 4096 groups × 4 peers = 16384 = ADDR_COUNT_LIMIT
for group in 0u16..4096 {
    for host in 1u8..=4 {
        let addr: Multiaddr = format!("/ip4/{}.{}.0.{}/tcp/8115",
            group >> 8, group & 0xff, host).parse().unwrap();
        // last_connected_at_ms=0, attempts_count=0 → is_connectable=true
        store.add_addr_fuzz(addr, Flags::COMPATIBILITY, 0, 0).unwrap();
    }
}
// Store is now at ADDR_COUNT_LIMIT with 4096 groups of exactly 4
let new_addr: Multiaddr = "/ip4/200.200.200.1/tcp/8115".parse().unwrap();
let result = store.add_addr(new_addr, Flags::COMPATIBILITY);
assert!(result.is_err()); // EvictionFailed — store permanently locked
``` [9](#0-8)

### Citations

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```

**File:** network/src/peer_store/peer_store_impl.rs (L75-75)
```rust
        self.check_purge()?;
```

**File:** network/src/peer_store/peer_store_impl.rs (L82-100)
```rust
    #[cfg(feature = "fuzz")]
    pub fn add_addr_fuzz(
        &mut self,
        addr: Multiaddr,
        flags: Flags,
        last_connected_at_ms: u64,
        attempts_count: u32,
    ) -> Result<()> {
        if self.ban_list.is_addr_banned(&addr) {
            return Ok(());
        }
        self.check_purge()?;
        let score = self.score_config.default_score;
        let mut addr_info = AddrInfo::new(addr, last_connected_at_ms, score, flags.bits());
        addr_info.attempts_count = attempts_count;

        self.addr_manager.add(addr_info);
        Ok(())
    }
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

**File:** network/src/peer_store/types.rs (L65-76)
```rust
    pub fn new(addr: Multiaddr, last_connected_at_ms: u64, score: Score, flags: u64) -> Self {
        AddrInfo {
            // only store tcp protocol
            addr: base_addr(&addr),
            score,
            last_connected_at_ms,
            last_tried_at_ms: 0,
            attempts_count: 0,
            random_id_pos: 0,
            flags,
        }
    }
```

**File:** network/src/peer_store/types.rs (L94-97)
```rust
        // we give up if never connect to this addr
        if self.last_connected_at_ms == 0 && self.attempts_count >= ADDR_MAX_RETRIES {
            return false;
        }
```
