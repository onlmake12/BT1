Audit Report

## Title
Peer Store Permanent DoS via Single-Group Address Flooding — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
`check_purge` computes `.take(len / 2)` where `len` is the count of distinct network groups. When all 16,384 stored addresses share a single `/16` subnet, `len == 1` and integer division yields `0`, so no addresses are evicted and `Err(PeerStoreError::EvictionFailed)` is returned. Because `add_addr` propagates this error unconditionally via `?`, the peer store permanently rejects every subsequent address insertion from any network group until the node is restarted.

## Finding Description

**Root cause — integer division in `check_purge`:**

`peers_by_network_group.len()` is captured as `len`, then `.take(len / 2)` is applied to the sorted group list. When `len == 1`, `1 / 2 == 0` in Rust integer arithmetic, so the iterator is immediately exhausted and `candidate_peers` is empty. [1](#0-0) 

**Unconditional error return:**

An empty `candidate_peers` after the network-group fallback causes an unconditional `Err(PeerStoreError::EvictionFailed)`. [2](#0-1) 

**Error propagation to every caller:**

`self.check_purge()?` in `add_addr` propagates the error to all callers, permanently blocking address insertion once the store is full and locked. [3](#0-2) 

**Why the first eviction step also fails:**

The first pass removes only addresses where `!is_connectable(now_ms)`. Freshly injected addresses have `last_connected_at_ms = 0` and `attempts_count = 0`. The third condition in `is_connectable` requires `attempts_count >= ADDR_MAX_FAILURES (10)`, which is false, so every injected address is considered connectable and none are removed. [4](#0-3) 

**Single network group for entire /16:**

All IPv4 addresses in `1.2.0.0/16` map to `Group::IP4([1, 2])`, producing exactly one group key. [5](#0-4) 

**Global address limit:**

`ADDR_COUNT_LIMIT = 16384`; a `/16` subnet has 65,536 addresses, making it trivially feasible to fill the store from a single subnet. [6](#0-5) 

## Impact Explanation

Once the store is locked, the node cannot record any new peer address from any network group. As existing connections drop naturally, the node cannot replenish its peer set and becomes progressively isolated from the CKB network. This constitutes an effective node crash from a network-participation standpoint, matching the **High** impact class: *Vulnerabilities which could easily crash a CKB node* (10,001–15,000 points).

## Likelihood Explanation

The attack requires only that an adversary deliver approximately 16,384 discovery `SendAddr` messages carrying addresses from a single `/16` subnet. This is achievable from a single controlled node with no proof-of-work, no key material, and no privileged access. The bug is deterministic: the same input always produces the same locked state. After a node restart the attacker can immediately repeat the flood.

## Recommendation

Replace the integer division with a minimum-of-one guard in `check_purge`:

```rust
// network/src/peer_store/peer_store_impl.rs, line 376
let take_count = (len / 2).max(1);
peers.into_iter().take(take_count)
```

Additionally, add a per-group cap in `add_addr` or `AddrManager::add` to prevent any single `/16` from monopolizing the store:

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
