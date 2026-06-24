All cited code is confirmed against the actual repository. Every claim in the report checks out:

**Claim 1 — /16 grouping:** `network_group.rs` lines 26-29 confirm `Group::IP4([bits[0], bits[1]])`. [1](#0-0) 

**Claim 2 — ADDR_COUNT_LIMIT = 16384:** `mod.rs` line 26 confirms the constant. [2](#0-1) 

**Claim 3 — Freshly added addresses are always connectable:** `AddrInfo::new` sets `last_connected_at_ms = 0` and `attempts_count = 0`. In `is_connectable`, the only path to `false` for `last_connected_at_ms == 0` requires `attempts_count >= ADDR_MAX_RETRIES (3)`, which is not satisfied at 0. [3](#0-2) 

**Claim 4 — Integer division bug:** `check_purge` phase-2 at line 376 uses `take(len / 2)`. When `len == 1`, this is `take(0)`, yielding an empty iterator. [4](#0-3) 

**Claim 5 — EvictionFailed propagation:** Lines 399-401 confirm the empty-candidate check, and line 75 confirms `check_purge()?` propagates the error out of `add_addr`. [5](#0-4) [6](#0-5) 

All five cited code paths are exact and correct. The vulnerability is real.

---

Audit Report

## Title
Integer Division by 2 in `check_purge` Phase-2 Yields Zero Eviction Candidates When All Peers Share One Network Group, Permanently Blocking `add_addr` — (`network/src/peer_store/peer_store_impl.rs`)

## Summary
When all 16384 peer store entries belong to a single `/16` network group, `peers_by_network_group.len()` equals 1, and `take(len / 2)` truncates to `take(0)` via integer division, producing no eviction candidates. Phase-1 also finds no candidates because freshly-added addresses have `attempts_count = 0 < ADDR_MAX_RETRIES = 3` and are always `is_connectable`. Every subsequent `add_addr` call returns `Err(EvictionFailed)`, permanently preventing new peer addresses from being stored until the node is restarted.

## Finding Description
In `check_purge` (`peer_store_impl.rs` line 327), the function first attempts phase-1 eviction: it collects all addresses where `is_connectable` returns `false`. Addresses inserted via `add_addr` are created with `AddrInfo::new(addr, 0, score, flags.bits())`, which sets `last_connected_at_ms = 0` and `attempts_count = 0`. In `is_connectable` (`types.rs` line 89), the only path to `false` for a never-connected address requires `attempts_count >= ADDR_MAX_RETRIES (3)`, which is not satisfied at 0. Phase-1 therefore finds zero candidates when the store is filled with freshly-added addresses.

Phase-2 then groups all stored addresses by network group key. For IPv4, the key is `Group::IP4([bits[0], bits[1]])` (`network_group.rs` line 28), meaning all addresses in any `/16` subnet share one key. With 16384 addresses from a single `/16`, `peers_by_network_group.len() == 1`. The code at line 376 then executes `take(len / 2)` = `take(1 / 2)` = `take(0)`, yielding an empty iterator. The `flat_map` produces nothing, `candidate_peers` is empty, and line 400 returns `Err(PeerStoreError::EvictionFailed)`. This error propagates through `add_addr`'s `self.check_purge()?` at line 75, causing every future `add_addr` call to fail.

A `/16` subnet provides 65536 distinct IP addresses, more than enough to fill the 16384-entry store. The attacker only needs to advertise these addresses via the discovery protocol from a single connected peer session.

## Impact Explanation
The node's peer store is permanently locked with attacker-controlled, unreachable addresses. All callers of `add_addr` — including the discovery protocol, identify protocol, and DNS seeding — receive `EvictionFailed` errors. The node cannot learn about new honest peers, outbound connection attempts are limited to the attacker's addresses (which are unreachable), and peer discovery is effectively halted. This constitutes a targeted node isolation attack achievable at negligible cost by any peer that can establish a single connection, matching **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, as the same technique applied at scale isolates multiple nodes simultaneously.

## Likelihood Explanation
Any unprivileged peer that can establish a single P2P connection to the victim can trigger this. No proof-of-work, key material, or special privileges are required. The attacker sends discovery messages advertising 16384 distinct addresses from one `/16` (e.g., `225.0.0.0`–`225.0.63.255` with varying ports). There is no per-group cap enforced during the filling phase. The attack is deterministic, cheap, and permanent until the node is restarted with a cleared peer store.

## Recommendation
Replace `take(len / 2)` with `take((len / 2).max(1))` at `peer_store_impl.rs` line 376 to ensure at least one group is always considered for eviction when the store is full. Additionally, enforce a per-group cap during `add_addr` (e.g., reject addresses whose group already has more than `ADDR_COUNT_LIMIT / expected_groups` entries) to prevent a single `/16` from monopolizing the store in the first place.

## Proof of Concept
```rust
// Fill store with 16384 addresses from 225.0.x.x (same /16 → Group::IP4([225, 0]))
for i in 0u32..16384 {
    let ip = Ipv4Addr::new(225, 0, (i / 256) as u8, (i % 256) as u8);
    let addr = format!("/ip4/{}/tcp/8115/p2p/QmFakeId{}", ip, i).parse().unwrap();
    peer_store.add_addr(addr, Flags::COMPATIBILITY).expect("fill should succeed");
}
// Store is now full; all entries share Group::IP4([225, 0])
// Attempt to add a legitimate peer address:
let new_addr = "/ip4/1.2.3.4/tcp/8115/p2p/QmLegitimate".parse().unwrap();
let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
assert!(
    result.is_err(),
    "Expected EvictionFailed, got: {:?}", result
);
// Verify: peers_by_network_group.len() == 1, take(1/2) == take(0), candidate_peers empty
```

### Citations

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

**File:** network/src/peer_store/types.rs (L89-97)
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
```

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
