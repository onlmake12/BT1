Audit Report

## Title
PeerStore Eviction Deadlock via Crafted /16-Group Flooding Permanently Blocks Peer Discovery — (`network/src/peer_store/peer_store_impl.rs`)

## Summary

`check_purge` contains a two-phase eviction strategy with a logical gap: Phase 2 only evicts from groups where `addrs.len() > 4`, and only from the top `len/2` groups by size. An attacker who fills the store with exactly 4 addresses per /16 network group causes both phases to find zero candidates, returning `Err(EvictionFailed)`. This error is silently discarded by `add_new_addrs`, permanently blocking new peer address ingestion for as long as the attacker maintains the crafted store state.

## Finding Description

**Root cause — `check_purge` Phase 2 threshold:** [1](#0-0) 

The condition `addrs.len() > 4` (strict greater-than) means groups of exactly 4 are never eviction candidates. Combined with `.take(len / 2)`, the bottom half of groups by size are also excluded. When the store is filled with 4096 groups of exactly 4 addresses each (totaling `ADDR_COUNT_LIMIT = 16384`), both conditions produce an empty candidate set. [2](#0-1) 

**Phase 1 also finds nothing:** Freshly injected addresses have `last_connected_at_ms=0`, `attempts_count=0`, `last_tried_at_ms=0`. `is_connectable` returns `true` for all of them because neither the retry threshold (`attempts_count >= ADDR_MAX_RETRIES=3`) nor the failure threshold (`attempts_count >= ADDR_MAX_FAILURES=10`) is met. [3](#0-2) 

**Result:** `check_purge` returns `Err(PeerStoreError::EvictionFailed)`. [4](#0-3) 

**Error is silently discarded:** `add_new_addrs` only `debug!`-logs the error and continues, with no disconnect, rate-limit, or propagation. [5](#0-4) 

**Exploit path:**
1. Attacker establishes one or more P2P connections to the victim node.
2. Attacker sends `DiscoveryMessage::Nodes` messages containing addresses from 4096 distinct /16 subnets (e.g., `1.0.x.x` through `16.15.x.x`), 4 addresses per subnet, all public IPs.
3. Each message carries up to `MAX_ADDR_TO_SEND=1000` nodes × `MAX_ADDRS=3` addresses = 3000 addresses; ~6 messages fill the store to 16384.
4. Once full, every subsequent `add_addr` call hits `check_purge`, both phases return empty, `EvictionFailed` is returned and discarded.
5. The victim node can no longer add any new peer addresses. Attacker periodically re-floods to replace any addresses that eventually become non-connectable (after 3 failed feeler attempts), sustaining the state indefinitely. [6](#0-5) 

## Impact Explanation

The victim node's peer store is permanently saturated with attacker-controlled addresses. New honest peer addresses received via the discovery protocol are silently dropped. The node cannot expand its peer set beyond its existing connections, degrading its ability to maintain a healthy outbound peer set and making it progressively more susceptible to eclipse attacks. This constitutes a suboptimal implementation of the CKB peer state storage mechanism exploitable by an unprivileged remote attacker, matching **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism**.

## Likelihood Explanation

- Requires only a standard P2P connection — no privileges, no PoW, no keys.
- The crafted address set (4096 /16 subnets × 4 public IPs each) is trivially constructable; the attacker does not need to own or control those IPs.
- ~6 `Nodes` messages suffice to fill the store; the protocol permits this without triggering any misbehavior detection.
- The error is silently swallowed at `debug!` level; the victim node has no observable signal and no automatic recovery mechanism.
- The attacker can use multiple sessions to accelerate filling and maintain the state with minimal bandwidth.

## Recommendation

1. **Fix the eviction threshold:** Change `addrs.len() > 4` to `addrs.len() >= 1` (or at minimum `>= 4`) so Phase 2 can always evict at least one address per group when the store is full.
2. **Evict from all groups, not just the top half:** Remove or increase the `.take(len / 2)` restriction so all groups are eviction candidates.
3. **Propagate or rate-limit on `EvictionFailed`:** Log at `warn!` level and consider rate-limiting or disconnecting sessions that repeatedly trigger a full-store condition.
4. **Per-session address admission limit:** Cap how many new addresses a single session can contribute to the store within a time window.

## Proof of Concept

```rust
// Fill PeerStore with 4096 /16 groups × 4 addrs each (all connectable, fresh)
let mut store = PeerStore::default();
for group in 0u16..4096 {
    let hi = (group >> 8) as u8;
    let lo = (group & 0xff) as u8;
    for host in 1u8..=4 {
        let addr = format!("/ip4/{}.{}.0.{}/tcp/8115/p2p/Qm...", hi, lo, host)
            .parse::<Multiaddr>().unwrap();
        store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
}
assert_eq!(store.addr_manager().count(), 16384);

// Any subsequent add_addr returns EvictionFailed, silently discarded by add_new_addrs
let new_addr = "/ip4/200.200.200.1/tcp/8115/p2p/Qm...".parse().unwrap();
let result = store.add_addr(new_addr, Flags::COMPATIBILITY);
assert!(matches!(result, Err(e) if e.to_string().contains("EvictionFailed")));
assert_eq!(store.addr_manager().count(), 16384); // unchanged
```

The existing `#[cfg(feature = "fuzz")] add_addr_fuzz` method in `peer_store_impl.rs` (lines 82–100) can be used to construct this state in a fuzz or integration test without needing live network connections.

### Citations

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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
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

**File:** network/src/protocols/discovery/mod.rs (L32-34)
```rust
const MAX_ADDR_TO_SEND: usize = 1000;
// The maximum number addresses in one Nodes item
const MAX_ADDRS: usize = 3;
```

**File:** network/src/protocols/discovery/mod.rs (L354-361)
```rust
            self.network_state.with_peer_store_mut(|peer_store| {
                if let Err(err) = peer_store.add_addr(addr.clone(), flags) {
                    debug!(
                        "Failed to add discovered address to peer_store {:?} {:?}",
                        err, addr
                    );
                }
            });
```
