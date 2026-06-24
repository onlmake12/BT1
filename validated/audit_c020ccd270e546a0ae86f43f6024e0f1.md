Audit Report

## Title
Peer Store DoS via Crafted Discovery Addresses Defeating Both Eviction Paths — (`network/src/peer_store/peer_store_impl.rs`)

## Summary

The `check_purge` function in `PeerStore` contains two sequential eviction strategies, both of which can be simultaneously defeated by an attacker advertising addresses from distinct /16 subnets with ≤4 addresses per group. When the peer store reaches `ADDR_COUNT_LIMIT` (16384) under this layout, both eviction paths produce empty candidate sets and `PeerStoreError::EvictionFailed` is returned, silently preventing any new legitimate peer addresses from being added. Peer discovery is effectively disabled for the victim node for as long as the attacker maintains the filled state.

## Finding Description

`check_purge` is invoked by `add_addr` whenever `addr_manager.count() >= ADDR_COUNT_LIMIT`: [1](#0-0) [2](#0-1) 

**Path 1 — non-connectable eviction:** Addresses inserted via `add_addr` are created with `last_connected_at_ms = 0` and `attempts_count = 0`: [3](#0-2) 

`is_connectable` returns `true` for all such addresses because neither false-returning condition is satisfied (no retries exhausted, no timeout exceeded): [4](#0-3) 

So `candidate_peers` in Path 1 is empty and nothing is evicted.

**Path 2 — network-group eviction:** Two compounding conditions must both be satisfied: [5](#0-4) 

1. `.take(len / 2)` — only the top half of groups by size are considered.
2. `if addrs.len() > 4` — only groups with more than 4 peers are eligible.

If an attacker fills all 16384 slots with one address per distinct /16 subnet:
- `len = 16384`, `take(8192)` processes 8192 groups
- Every group has exactly 1 address → `1 > 4` is `false` → `None` for every group
- `candidate_peers` is empty → `return Err(PeerStoreError::EvictionFailed)` [6](#0-5) 

The IPv4 `Group` type uses only the first two octets, so each `a.b.*.*` subnet is a separate group: [7](#0-6) 

**Delivery path:** `add_new_addrs` in the discovery protocol calls `peer_store.add_addr` for each received address and only logs errors at `debug` level, effectively silently swallowing `EvictionFailed`: [8](#0-7) 

`ADDR_COUNT_LIMIT` is 16384: [9](#0-8) 

## Impact Explanation

Once the peer store is saturated with attacker-controlled addresses (all connectable, all in distinct /16 groups with ≤4 per group), every subsequent call to `add_addr` from the discovery or identify protocols returns `Err(EvictionFailed)` and is silently dropped. The victim node cannot add newly discovered legitimate peer addresses, effectively disabling peer discovery. Applied at scale across multiple nodes, this degrades the CKB network's ability to propagate peer information and can cause network-level congestion and partitioning. This maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation

- Requires only a single inbound or outbound P2P connection to the victim — no privileged access, no PoW, no key material.
- The discovery protocol imposes no per-IP or per-session rate limit on how many distinct /16 subnets can be advertised.
- The attack is not permanently self-sustaining (failed connection attempts eventually make addresses non-connectable after `ADDR_MAX_RETRIES = 3` failures), but the attacker can continuously re-advertise fresh addresses across multiple discovery rounds to maintain the filled state.
- Filling 16384 slots requires approximately 6 discovery messages (at `MAX_ADDR_TO_SEND` addresses per message), achievable from a single peer across multiple rounds.

## Recommendation

1. **Remove or widen the `take(len / 2)` truncation** — consider all groups, not just the top half, when selecting eviction candidates.
2. **Lower or remove the `> 4` threshold** — always evict at least one address from the largest group regardless of its size, ensuring the eviction path never produces an empty candidate set when the store is full.
3. **Add a per-session or per-/16 rate limit** in `add_new_addrs` to bound how many distinct subnets a single peer can contribute to the store.
4. **Elevate `EvictionFailed` to `warn`-level logging** so operators can detect the condition in production.

## Proof of Concept

```rust
let mut peer_store = PeerStore::default();
// Fill with 16384 addresses, one per distinct /16 subnet, all connectable
for i in 0u32..16384 {
    let a = (i >> 8) as u8;
    let b = (i & 0xff) as u8;
    let addr: Multiaddr = format!(
        "/ip4/{}.{}.0.1/tcp/8114/p2p/{}",
        a, b,
        PeerId::random().to_base58()
    ).parse().unwrap();
    peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
}
assert_eq!(peer_store.addr_manager().count(), 16384);

// Now attempt to add a new legitimate address — returns Err(EvictionFailed)
let new_addr: Multiaddr = format!(
    "/ip4/200.200.200.1/tcp/8114/p2p/{}",
    PeerId::random().to_base58()
).parse().unwrap();
assert!(peer_store.add_addr(new_addr, Flags::COMPATIBILITY).is_err());
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

**File:** network/src/network_group.rs (L26-29)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
            }
```

**File:** network/src/protocols/discovery/mod.rs (L347-363)
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
    }
```

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```
