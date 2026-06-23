I have all the code I need. Let me trace the exact path carefully.

### Title
Peer Store Permanently Blocked by Single-Group Address Flood via `check_purge` Integer Division Bug — (`network/src/peer_store/peer_store_impl.rs`)

### Summary

The `check_purge` phase-2 eviction path contains an integer division bug: `take(len / 2)` where `len` is the number of distinct network groups. When all 16,384 peer store entries belong to a single `/16` network group, `len == 1` and `1 / 2 == 0` in integer arithmetic, so `take(0)` yields an empty iterator. No candidates are evicted, `candidate_peers.is_empty()` is true, and `Err(PeerStoreError::EvictionFailed)` is returned. Because `add_addr` propagates this error, the peer store permanently rejects every subsequent address insertion.

### Finding Description

**Root cause — `check_purge` phase 2:** [1](#0-0) 

```
let len = peers_by_network_group.len();   // == 1 when all addrs share one /16
...
peers.into_iter()
    .take(len / 2)   // take(0) — zero groups selected
```

When `len == 1`, `take(0)` produces an empty iterator. The `flat_map` closure never runs, `candidate_peers` is empty, and line 400 returns `Err(EvictionFailed)`. [2](#0-1) 

**Network group granularity — `/16` for IPv4:** [3](#0-2) 

All addresses `A.B.x.x` map to `Group::IP4([A, B])`. A single attacker controlling addresses in one `/16` block fills the store with one group.

**Freshly discovered addresses are always connectable (phase 1 never evicts them):**

`add_addr` stores addresses with `last_connected_at_ms = 0`, `attempts_count = 0`, `last_tried_at_ms = 0`. [4](#0-3) 

`is_connectable` returns `true` for these because `attempts_count (0) < ADDR_MAX_RETRIES (3)` and `attempts_count (0) < ADDR_MAX_FAILURES (10)`: [5](#0-4) 

Phase 1 finds zero non-connectable entries and skips removal, entering phase 2 with the broken `take(0)` path.

**Discovery protocol entry point:**

A remote peer sends `Nodes` messages. `add_new_addrs` calls `add_addr` for each address and silently swallows the error: [6](#0-5) 

The error is only `debug!`-logged; no disconnect, no rate-limit, no backpressure.

**Per-message address capacity:**

`MAX_ADDR_TO_SEND = 1000` items × `MAX_ADDRS = 3` addresses per item = up to 3,000 addresses per non-announce `Nodes` message. [7](#0-6) 

To fill 16,384 slots the attacker needs ~6 simultaneous connections (each sending one non-announce `Nodes` burst), or fewer connections using repeated announce messages over time.

### Impact Explanation

Once the peer store is saturated with single-group addresses, every call to `add_addr` returns `Err(EvictionFailed)`. The node can no longer learn about new peers via the discovery protocol. Its outbound connection pool will eventually exhaust the 16,384 attacker-controlled addresses (none of which respond), and the node cannot replenish it. Inbound connections still work, but the node loses autonomous peer discovery and outbound connection diversity, degrading its ability to participate in the network and making it susceptible to eclipse attacks.

### Likelihood Explanation

The attack requires only a standard P2P connection and the ability to advertise public IP addresses from a single `/16` block. No privileged access, no PoW, no key material is needed. The `is_reachable` filter blocks private/loopback ranges but accepts any routable public IP. An attacker with a /16 allocation (or spoofed addresses in one) can execute this with ~6 connections. The `received_nodes` guard prevents a single session from sending more than one non-announce burst, but multiple sessions bypass it trivially.

### Recommendation

Replace `take(len / 2)` with a minimum-of-1 guard:

```rust
peers.into_iter()
    .take((len / 2).max(1))   // always consider at least one group
    ...
```

Additionally, enforce a per-`/16` cap during `add_addr` (e.g., reject addresses whose network group already has ≥ N entries) so the store cannot be monopolised by a single group in the first place.

### Proof of Concept

```rust
#[test]
fn test_single_group_eviction_failure() {
    let mut peer_store = PeerStore::default();
    // Fill store with ADDR_COUNT_LIMIT addresses all in 1.2.0.0/16
    for i in 0u32..ADDR_COUNT_LIMIT as u32 {
        let a = ((i >> 8) & 0xff) as u8;
        let b = (i & 0xff) as u8;
        let addr: Multiaddr = format!(
            "/ip4/1.2.{}.{}/tcp/8114/p2p/{}",
            a, b, PeerId::random().to_base58()
        ).parse().unwrap();
        peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
    }
    // 16385th address from a different /16 must fail
    let new_addr: Multiaddr = format!(
        "/ip4/9.9.9.9/tcp/8114/p2p/{}", PeerId::random().to_base58()
    ).parse().unwrap();
    assert!(peer_store.add_addr(new_addr, Flags::COMPATIBILITY).is_err());
}
```

### Citations

**File:** network/src/peer_store/peer_store_impl.rs (L76-79)
```rust
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

**File:** network/src/network_group.rs (L26-29)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
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

**File:** network/src/protocols/discovery/mod.rs (L32-34)
```rust
const MAX_ADDR_TO_SEND: usize = 1000;
// The maximum number addresses in one Nodes item
const MAX_ADDRS: usize = 3;
```

**File:** network/src/protocols/discovery/mod.rs (L347-362)
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
```
