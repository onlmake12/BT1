Audit Report

## Title
Peer Store Permanent Lock via Single-Group Address Flooding — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
`check_purge`'s network-group fallback computes `take(len / 2)` where `len` is the number of distinct network groups. When all stored addresses belong to a single `/16` subnet, `len = 1` and `1 / 2 = 0` (integer division), so `take(0)` produces an empty iterator, `candidate_peers` is empty, and the function returns `Err(PeerStoreError::EvictionFailed)`. Because `add_addr` propagates this error via `?`, the peer store becomes permanently locked: no address from any network group can be added until the node is restarted.

## Finding Description

**Root cause — `check_purge` step 2 (`peer_store_impl.rs` L366–400):**

```rust
let len = peers_by_network_group.len();   // = 1 when all addrs share one /16
peers.into_iter()
    .take(len / 2)                        // take(0) — empty iterator
    .flat_map(…)
    .collect()                            // candidate_peers = []
// …
if candidate_peers.is_empty() {
    return Err(PeerStoreError::EvictionFailed.into());
}
``` [1](#0-0) 

**Step 1 does not help.** Freshly injected addresses have `last_connected_at_ms = 0`, `attempts_count = 0`, `last_tried_at_ms = 0`. Walking `is_connectable`:
- `tried_in_last_minute`: `0 >= now_ms − 60_000` → false (now_ms ≈ 1.7 × 10¹²).
- `last_connected_at_ms == 0 && attempts_count >= ADDR_MAX_RETRIES (3)`: `0 >= 3` → false.
- `now_ms.saturating_sub(0) > ADDR_TIMEOUT_MS && attempts_count >= ADDR_MAX_FAILURES (10)`: second clause `0 >= 10` → false.

All freshly injected addresses return `true` from `is_connectable`, so step 1 evicts nothing.

**Network-group key.** IPv4 addresses are bucketed by the first two octets only:

```rust
if let IpAddr::V4(ipv4) = ip_addr {
    let bits = ipv4.octets();
    return Group::IP4([bits[0], bits[1]]);
}
``` [2](#0-1) 

Every address in `1.2.0.0/16` maps to `Group::IP4([1, 2])` — a single group.

**Error propagation.** `add_addr` calls `self.check_purge()?`, so `EvictionFailed` is returned directly to the caller: [3](#0-2) 

**Capacity limit.** `ADDR_COUNT_LIMIT = 16_384`: [4](#0-3) 

A `/16` subnet contains 65,536 addresses, so supplying 16,384 distinct addresses from one `/16` is trivially feasible.

## Impact Explanation

Once the store is at capacity with a single network group, every subsequent `add_addr` call returns `Err(EvictionFailed)`. The node cannot record any new peer address from any network group. As existing connections drop, the node cannot replenish its peer set and becomes progressively isolated from the CKB network. This matches the **High** impact class: *Vulnerabilities which could easily crash a CKB node* (network isolation is functionally equivalent to a node being unable to participate in the network).

## Likelihood Explanation

The attack requires only that an adversary delivers ~16,384 discovery `SendAddr` messages carrying addresses from a single `/16` subnet. This is achievable from a single controlled node with no PoW, no key material, and no privileged access. The bug is deterministic: the outcome is guaranteed whenever the store fills with a single network group. The attacker can repeat the flood after each node restart.

## Recommendation

Replace the `len / 2` integer division with a minimum-of-one guard in `check_purge`:

```rust
let take_count = (len / 2).max(1);
peers.into_iter().take(take_count) …
```

Additionally, add a per-group cap during insertion in `add_addr` or `AddrManager::add`:

```rust
const MAX_ADDRS_PER_GROUP: usize = 256;
if group_count >= MAX_ADDRS_PER_GROUP { return Ok(()); }
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
// Store is now at ADDR_COUNT_LIMIT with exactly 1 network group
// Any subsequent add_addr — even from a completely different /16 — must fail
let new_addr: Multiaddr = "/ip4/9.9.9.9/tcp/8115".parse().unwrap();
assert!(peer_store.add_addr(new_addr, Flags::COMPATIBILITY).is_err());
// Assertion passes: check_purge computes len=1, len/2=0, evicts nothing, returns Err(EvictionFailed)
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

**File:** network/src/peer_store/peer_store_impl.rs (L366-401)
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
