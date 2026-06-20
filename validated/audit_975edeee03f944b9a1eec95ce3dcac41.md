The code is fully readable. Let me trace the exact logic.

## Analysis

### The Bug: `take(len / 2)` with `len = 1`

In `check_purge`, when no non-connectable peers exist, the fallback path groups peers by network segment and calls `.take(len / 2)`: [1](#0-0) 

When all 16384 entries share one `/16` group (`Group::IP4([A, B])`):
- `peers_by_network_group.len()` = **1**
- `len / 2` = **0** (Rust integer division)
- `.take(0)` yields an empty iterator
- `candidate_peers` is empty → `Err(PeerStoreError::EvictionFailed)` at line 400 [2](#0-1) 

### Attacker Entry Path

The discovery protocol's `add_new_addrs` calls `peer_store.add_addr()` for every received address with no per-subnet cap: [3](#0-2) 

The only filter is `is_valid_addr` → `is_reachable()`, which passes for any public IPv4. A single connected peer can send up to `MAX_ADDR_TO_SEND = 1000` addresses per Nodes message: [4](#0-3) 

Filling 16384 slots requires ~17 such messages, all with addresses in `1.2.x.x`. Freshly added addresses have `last_connected_at_ms = 0` and `attempts_count = 0`, so they pass `is_connectable`: [5](#0-4) 

### Group Mapping Confirmation

All `1.2.x.x` addresses map to the same `Group::IP4([1, 2])`: [6](#0-5) 

`ADDR_COUNT_LIMIT` is exactly 16384: [7](#0-6) 

---

### Title
**Peer store permanently blocked by single-subnet flood via discovery protocol — (`network/src/peer_store/peer_store_impl.rs`)**

### Summary
An integer division bug in `check_purge` causes `Err(EvictionFailed)` when all peer store entries belong to a single `/16` network group. An attacker with one P2P connection can exploit this via the discovery protocol to permanently prevent the victim node from adding any new peer addresses.

### Finding Description
`check_purge` uses `take(len / 2)` to select the top half of network groups for eviction candidates. When `len = 1` (all 16384 entries in one group), `1 / 2 = 0` in integer arithmetic, so `.take(0)` produces an empty iterator. No eviction candidates are found, and the function returns `Err(EvictionFailed)`. This error propagates out of `add_addr`, silently dropping all future peer address additions.

The root cause is at: [8](#0-7) 

The group with 16384 entries clearly satisfies `addrs.len() > 4` (line 378), but it is never reached because `take(0)` skips it entirely.

### Impact Explanation
Once the peer store is poisoned:
- `add_addr` returns `Err` for every new address (discovery, identify protocol, etc.)
- The node's peer store remains filled with 16384 attacker-controlled fake addresses
- When existing connections drop, the node fetches outbound dial candidates exclusively from the poisoned peer store — all of which are non-responsive attacker addresses
- The node becomes progressively isolated from the honest network, enabling an eclipse attack and potential consensus deviation

### Likelihood Explanation
The attacker needs only one accepted P2P connection. With `MAX_ADDR_TO_SEND = 1000` per Nodes message, ~17 messages suffice to fill the store. The `/16` subnet `1.2.0.0/16` has 65536 addresses, all public and `is_reachable`-passing. No special privileges, no PoW, no Sybil majority required.

### Recommendation
Replace `take(len / 2)` with `take(len.saturating_add(1) / 2)` (ceiling division) or `take(len.max(1))` so that a single-group store still yields eviction candidates. Additionally, enforce a per-`/16`-subnet cap during `add_addr` to prevent the store from ever being monopolized by one group.

### Proof of Concept
```rust
let mut peer_store = PeerStore::default();
// Fill with 16384 addresses all in 1.2.x.x (/16 group IP4([1,2]))
for i in 0..ADDR_COUNT_LIMIT {
    let port = (i % 65535) + 1;
    let third = (i / 256) % 256;
    let fourth = i % 256;
    let addr: Multiaddr = format!(
        "/ip4/1.2.{}.{}/tcp/{}/p2p/{}",
        third, fourth, port, PeerId::random().to_base58()
    ).parse().unwrap();
    peer_store.add_addr(addr, Flags::COMPATIBILITY).unwrap();
}
// Now try to add an address from a completely different subnet
let honest_addr: Multiaddr = format!(
    "/ip4/3.4.5.6/tcp/8888/p2p/{}", PeerId::random().to_base58()
).parse().unwrap();
// Returns Err(EvictionFailed) — peer store permanently blocked
assert!(peer_store.add_addr(honest_addr, Flags::COMPATIBILITY).is_err());
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

**File:** network/src/protocols/discovery/mod.rs (L32-32)
```rust
const MAX_ADDR_TO_SEND: usize = 1000;
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

**File:** network/src/network_group.rs (L26-28)
```rust
            if let IpAddr::V4(ipv4) = ip_addr {
                let bits = ipv4.octets();
                return Group::IP4([bits[0], bits[1]]);
```

**File:** network/src/peer_store/mod.rs (L26-26)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
```
