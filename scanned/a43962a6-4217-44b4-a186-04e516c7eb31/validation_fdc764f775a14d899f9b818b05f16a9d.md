Audit Report

## Title
`check_purge` Integer-Division Zero-Eviction Bug Enables Permanent Peer Store DoS via Single-Group Flood — (`network/src/peer_store/peer_store_impl.rs`)

## Summary

In `check_purge`, the expression `take(len / 2)` at line 376 evaluates to `take(0)` when all 16 384 peer store entries belong to a single network group (`len == 1`), producing zero eviction candidates and returning `Err(PeerStoreError::EvictionFailed)`. An unprivileged remote peer can reach this state by flooding discovery `Nodes` messages with addresses from a single IPv4 /16 prefix, permanently preventing the victim node from recording any new peer addresses and isolating it from the honest peer graph.

## Finding Description

**Root cause — integer division truncation at line 376**

`check_purge` is entered whenever `addr_manager.count() >= ADDR_COUNT_LIMIT (16384)`. The first eviction pass collects non-connectable entries. Entries inserted by `add_addr` are created with `AddrInfo::new(addr, 0, score, flags)`, setting `last_connected_at_ms = 0` and `attempts_count = 0`. Evaluating `is_connectable` for such an entry:

- `tried_in_last_minute`: `last_tried_at_ms (0) >= now_ms − 60 000` → false (now_ms ≈ 1.7 × 10¹² ms)
- `last_connected_at_ms == 0 && attempts_count >= ADDR_MAX_RETRIES (3)`: `0 >= 3` → false
- `now_ms − 0 > ADDR_TIMEOUT_MS && attempts_count >= ADDR_MAX_FAILURES (10)`: `0 >= 10` → false
- Returns `true`

All attacker-injected entries are therefore connectable; the first eviction pass removes nothing and `candidate_peers.is_empty()` is true, falling through to the network-group path.

In the network-group path, all 16 384 addresses sharing the same first two IPv4 octets (e.g., `1.2.x.x`) map to the identical `Group::IP4([1, 2])` key (confirmed in `network_group.rs` lines 26–28), so `peers_by_network_group.len() == 1`. Then:

```rust
let len = peers_by_network_group.len();  // 1
peers.into_iter().take(len / 2)          // take(0) — empty
```

`1 / 2 == 0` in Rust integer arithmetic. The iterator is immediately exhausted, `candidate_peers` is empty, and the function returns `Err(PeerStoreError::EvictionFailed)`.

**Attacker entry point — `add_new_addrs` in discovery**

`add_new_addrs` iterates every received address and calls `peer_store.add_addr` with no per-session or per-source-IP quota. The only filter is `is_valid_addr`, which calls `is_reachable` and passes any globally routable address. Addresses in a publicly routable /16 block (e.g., `1.2.0.0/16`) pass this filter. `AddrManager.add` deduplicates by exact multiaddr, so the attacker needs 16 384 distinct addresses (different ports or host addresses within the /16 — trivially achievable with 65 536 available host addresses).

**Why existing guards are insufficient**

- The ban list check in `add_addr` only blocks explicitly banned addresses; it does not limit per-group density.
- The `addrs.len() > 4` guard inside `flat_map` (line 378) is never reached because `take(0)` prevents any group from being iterated.
- There is no per-session rate limit or per-network-group cap in `add_new_addrs`.

## Impact Explanation

Once the peer store holds 16 384 entries all in one /16 group, every subsequent call to `add_addr` returns `Err(EvictionFailed)`. The node can no longer store any newly discovered peer addresses. `fetch_addrs_to_attempt` returns nothing useful (all injected entries have `last_connected_at_ms = 0`, failing the `t > addr_expired_ms` filter). `fetch_addrs_to_feeler` returns only attacker-controlled addresses (entries with `last_connected_at_ms = 0` pass `!peer_addr.connected(|t| t > addr_expired_ms)`). The victim node is effectively isolated from the honest peer graph, satisfying the precondition for an eclipse attack. This matches the **High** impact class: a vulnerability that could easily isolate a CKB node from the network, and is a direct prerequisite for consensus deviation.

## Likelihood Explanation

- No privilege is required beyond completing a P2P handshake.
- Filling 16 384 slots requires sending discovery `Nodes` messages containing addresses from a single /16 block. With up to 3 000 addresses per non-announce message, approximately 6 messages across 6 sessions suffice.
- Globally routable /16 blocks are abundant; `is_valid_addr` does not restrict them.
- The attack is repeatable: if the victim node restarts without clearing its peer store, the injected entries persist.

## Recommendation

Replace `take(len / 2)` with a guard that always selects at least one group when `len >= 1`:

```rust
let take_count = std::cmp::max(1, len / 2);
peers.into_iter().take(take_count)...
```

Additionally:
- Enforce a per-network-group cap (e.g., no more than `ADDR_COUNT_LIMIT / 16` entries per /16 group) in `add_addr` or `AddrManager::add`.
- Add a per-session or per-source-IP rate limit in `add_new_addrs`.

## Proof of Concept

```rust
// Minimal unit test demonstrating the bug
#[test]
fn test_check_purge_single_group_eviction_failure() {
    let mut peer_store: PeerStore = Default::default();
    // Fill store with 16384 addresses all in 1.2.x.x/16 via add_addr
    // (last_connected_at_ms=0, attempts_count=0 → all connectable)
    for i in 0u32..16384 {
        let ip = format!("1.2.{}.{}", (i >> 8) & 0xff, i & 0xff);
        let addr: Multiaddr = format!("/ip4/{}/tcp/8114", ip).parse().unwrap();
        // First 16383 succeed (count < ADDR_COUNT_LIMIT); 16384th also succeeds
        let _ = peer_store.add_addr(addr, Flags::COMPATIBILITY);
    }
    // 16385th triggers check_purge with count==16384, len==1 → take(0) → EvictionFailed
    let new_addr: Multiaddr = "/ip4/5.6.7.8/tcp/8114".parse().unwrap();
    let result = peer_store.add_addr(new_addr, Flags::COMPATIBILITY);
    assert!(matches!(result, Err(e) if e.to_string().contains("EvictionFailed")));
    // Feeler list returns only attacker addresses
    let feelers = peer_store.fetch_addrs_to_feeler(10, |_| true);
    assert!(feelers.iter().all(|a| a.addr.to_string().contains("1.2.")));
}
```

The key lines in the actual code confirming the bug: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** network/src/peer_store/mod.rs (L26-35)
```rust
pub(crate) const ADDR_COUNT_LIMIT: usize = 16384;
/// Consider we never seen a peer if peer's last_connected_at beyond this timeout
const ADDR_TIMEOUT_MS: u64 = 7 * 24 * 3600 * 1000;
/// The timeout that peer's address should be added to the feeler list again
pub(crate) const ADDR_TRY_TIMEOUT_MS: u64 = 3 * 24 * 3600 * 1000;
/// When obtaining the list of selectable nodes for identify,
/// the node that has just been disconnected needs to be excluded
pub(crate) const DIAL_INTERVAL: u64 = 15 * 1000;
const ADDR_MAX_RETRIES: u32 = 3;
const ADDR_MAX_FAILURES: u32 = 10;
```
