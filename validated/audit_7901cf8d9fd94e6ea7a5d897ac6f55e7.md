All cited code verified against the actual repository. Every claim checks out:

- `add_addr` at L77-78 passes `0` as `last_connected_at_ms` and `attempts_count` defaults to `0`, so pass 1 finds no eviction candidates among attacker-injected entries. [1](#0-0) 
- Pass 2 at L376 uses `len / 2` integer division; with `len == 1`, `.take(0)` yields an empty iterator. [2](#0-1) 
- `EvictionFailed` is returned at L399-401 when `candidate_peers` is empty. [3](#0-2) 
- `is_connectable` only returns `false` when `attempts_count >= 3` (never connected) or `>= 10` (timed out), neither of which applies to fresh attacker entries. [4](#0-3) 
- Network group is /16 (`IP4([bits[0], bits[1]])`). [5](#0-4) 
- Discovery handler swallows the error at `debug` level. [6](#0-5) 
- Store capacity is 16384. [7](#0-6) 

---

Audit Report

## Title
Peer Store Permanently Blocked by Single-/16-Group Flood via Discovery Nodes — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
`check_purge`'s second-pass eviction computes `len / 2` to determine how many network groups to evict from. When all 16,384 peer store slots are occupied by addresses sharing one /16 group (`len == 1`), integer division yields `0`, no candidates are selected, and `EvictionFailed` is returned. Because `add_addr` is invoked unconditionally for every Discovery-received address and errors are only logged at `debug` level, a single connected peer can flood the store with 16,384 addresses from one /16 block and permanently prevent the node from learning new peers.

## Finding Description
**Pass 1 (`is_connectable` filter, L341–355):** `add_addr` constructs every discovered entry with `last_connected_at_ms = 0` and `attempts_count = 0`. `is_connectable` returns `false` only when `attempts_count >= ADDR_MAX_RETRIES (3)` with no prior connection, or `attempts_count >= ADDR_MAX_FAILURES (10)` after timeout. Fresh attacker entries satisfy neither condition, so pass 1 produces zero eviction candidates and falls through to pass 2.

**Pass 2 (network-group eviction, L358–401):** All addresses are bucketed by `Group`. For IPv4, the group key is `IP4([octet0, octet1])` — a /16 block. If all 16,384 entries share one /16, `peers_by_network_group.len() == 1`. The code then does:
```rust
let len = peers_by_network_group.len();   // 1
peers.into_iter().take(len / 2)           // take(0) — empty
```
`candidate_peers` is empty, and the function returns `Err(PeerStoreError::EvictionFailed)`.

**Silent swallowing:** `add_new_addrs` in the Discovery handler calls `peer_store.add_addr(...)` inside a closure and only logs failures at `debug` level, so the caller never observes the error and continues processing subsequent addresses.

**Capacity:** `ADDR_COUNT_LIMIT = 16384`. A /16 block provides 65,536 unique IPs, more than sufficient to fill every slot.

## Impact Explanation
Once the store is saturated, every subsequent `add_addr` call returns `EvictionFailed`. The node cannot learn new peers via Discovery or Identify. As existing connections close naturally, the node cannot replace them and progressively loses all connectivity. This is a remotely triggerable, persistent node isolation with no recovery path short of a restart and peer store deletion. **Impact: High — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation
Any peer that completes the standard P2P handshake can send repeated Discovery `Nodes` messages. Each message may carry many addresses; all addresses need only share the same /16 (e.g., `1.2.x.x`) with distinct peer IDs. No privileged access is required. The `is_valid_addr` / `is_reachable` filter is satisfied by any public /16 range. 16,384 entries are reachable with sustained but modest message volume from a single malicious peer.

## Recommendation
Replace `len / 2` with `(len + 1) / 2` (ceiling division) so that at least one group is always processed when `len >= 1`:
```rust
.take((len + 1) / 2)
```
Additionally, enforce a per-/16 admission cap inside `add_addr` to prevent any single network group from monopolizing the store.

## Proof of Concept
```rust
let mut peer_store = PeerStore::default();
// Fill with 16384 entries all from 1.2.x.x (/16 group IP4([1, 2]))
for i in 0u32..16384 {
    let ip = std::net::Ipv4Addr::new(1, 2, (i >> 8) as u8, i as u8);
    let peer_id = PeerId::random();
    let addr: Multiaddr = format!("/ip4/{}/tcp/8114/p2p/{}", ip, peer_id.to_base58())
        .parse().unwrap();
    peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
}
// Store is now full with a single /16 group (len == 1 in check_purge pass 2)
let new_peer_id = PeerId::random();
let new_addr: Multiaddr = format!("/ip4/8.8.8.8/tcp/8114/p2p/{}", new_peer_id.to_base58())
    .parse().unwrap();
// Returns EvictionFailed — peer store is permanently blocked
assert!(peer_store.add_addr(new_addr, Flags::COMPATIBILITY).is_err());
```

### Citations

**File:** network/src/peer_store/peer_store_impl.rs (L77-78)
```rust
        self.addr_manager
            .add(AddrInfo::new(addr, 0, score, flags.bits()));
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

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```
