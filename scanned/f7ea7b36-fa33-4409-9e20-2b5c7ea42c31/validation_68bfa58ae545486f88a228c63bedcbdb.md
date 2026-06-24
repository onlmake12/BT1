Audit Report

## Title
Onion3 `is_connectable` Bypass in `fetch_random` Permanently Pollutes Feeler Connection Pool — (File: `network/src/peer_store/addr_manager.rs`)

## Summary
`fetch_random` unconditionally returns Onion3 addresses regardless of `is_connectable` status via a logical OR bypass. An attacker relaying fake Onion3 multiaddrs via P2P discovery causes the node's feeler service to exhaust its 10 slots on permanently dead addresses, degrading peer discovery indefinitely without requiring ongoing attacker presence.

## Finding Description

In `fetch_random`, the `None` branch (triggered when `multiaddr_to_socketaddr` returns `None`, which is always the case for Onion3 addresses) uses a logical OR that bypasses `is_connectable`: [1](#0-0) 

The regular IP branch (lines 66–72) enforces `is_connectable` strictly, but the `None` branch does not.

`is_connectable` permanently rejects an address when `last_connected_at_ms == 0 && attempts_count >= ADDR_MAX_RETRIES`: [2](#0-1) 

`ADDR_MAX_RETRIES` is 3: [3](#0-2) 

`add_addr` (the P2P discovery ingress path) creates `AddrInfo` with `last_connected_at_ms = 0` and `attempts_count = 0`: [4](#0-3) 

`dial_feeler` calls `fetch_addrs_to_feeler` → `fetch_random`, then immediately calls `mark_tried` on every returned address: [5](#0-4) 

The `fetch_addrs_to_feeler` filter checks only `!tried_in_last_minute` and `!connected(within 3 days)` — it does **not** check `is_connectable`. Onion3 addresses with `last_connected_at_ms = 0` pass both conditions: [6](#0-5) 

`fetch_addrs_to_attempt` requires `last_connected_at_ms > addr_expired_ms`, which Onion3 addresses with `last_connected_at_ms = 0` cannot satisfy, so only the feeler path is affected: [7](#0-6) 

**Complete attack cycle:**
1. Attacker relays N fake Onion3 multiaddrs via P2P discovery → `add_addr` stores them with `attempts_count = 0`, `last_connected_at_ms = 0`.
2. Feeler fires → `fetch_addrs_to_feeler` → `fetch_random` returns Onion3 addresses (pass feeler filter, pass Onion3 bypass) → `mark_tried` increments `attempts_count`.
3. After 3 feeler cycles (≥3 minutes), `attempts_count = 3`, `last_connected_at_ms = 0` → `is_connectable` returns `false`.
4. Next feeler cycle: `tried_in_last_minute` is now false (>1 min elapsed) → feeler filter passes → `fetch_random` Onion3 bypass passes → address returned again → `mark_tried` called again.
5. Loop continues indefinitely. The feeler's `FEELER_CONNECTION_COUNT = 10` slots are consumed by permanently dead Onion3 addresses.

The `check_purge` eviction logic does remove non-connectable addresses (using `is_connectable` directly, without the Onion3 bypass), but only triggers when the store reaches `ADDR_COUNT_LIMIT` (16384): [8](#0-7) 

If the attacker injects fewer than 16384 addresses, `check_purge` never fires and the dead addresses persist indefinitely.

## Impact Explanation

The feeler service is the primary mechanism for validating and discovering new peers. With all 10 feeler slots consumed by attacker-injected dead Onion3 addresses, the node cannot validate or onboard new reachable peers. This constitutes a suboptimal implementation of the CKB peer store mechanism and a degradation of peer discovery availability, matching **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism**, as the peer store is the node's address/state storage for network topology.

## Likelihood Explanation

- Onion3 multiaddrs are valid multiaddr format and are accepted by `add_addr` without special filtering.
- The P2P discovery protocol is an unauthenticated, unprivileged path — any connected peer can relay addresses.
- No Tor support is required on the victim node; the feeler will attempt to dial and fail, incrementing `attempts_count` regardless.
- The cycle is self-sustaining below the 16384 address limit: no ongoing attacker presence is needed after the initial injection.

## Recommendation

Remove the unconditional Onion3 bypass. Onion3 addresses that have exhausted retries should be treated the same as any other permanently non-connectable address:

```rust
None => {
    if filter(&addr_info) && addr_info.is_connectable(now_ms) {
        addr_infos.push(addr_info);
    }
}
```

If Onion3 addresses genuinely require special liveness semantics (e.g., because Tor circuits are intermittent), a separate, bounded retry budget distinct from `ADDR_MAX_RETRIES` should be used, rather than bypassing `is_connectable` entirely.

## Proof of Concept

```rust
// Unit test (no Tor required)
let mut addr_manager = AddrManager::default();
let now_ms = ckb_systemtime::unix_time_as_millis();

for i in 0..20u8 {
    let onion_addr: Multiaddr = format!(
        "/onion3/{}:{}/p2p/{}",
        "a".repeat(56), i, PeerId::random().to_base58()
    ).parse().unwrap();
    let mut info = AddrInfo::new(onion_addr, 0, 100, Flags::COMPATIBILITY.bits());
    // Simulate 3 failed feeler attempts
    info.attempts_count = ADDR_MAX_RETRIES; // last_connected_at_ms stays 0
    addr_manager.add(info);
}

let results = addr_manager.fetch_random(20, |_| true);
// All 20 are returned despite is_connectable() == false for every one
assert_eq!(results.len(), 20);
for r in &results {
    assert!(!r.is_connectable(now_ms)); // confirms the bypass
}
```

### Citations

**File:** network/src/peer_store/addr_manager.rs (L74-90)
```rust
                None => {
                    if filter(&addr_info) {
                        if addr_info.is_connectable(now_ms)
                            || addr_info
                                .addr
                                .iter()
                                .any(|p| matches!(p, Protocol::Onion3(_)))
                        {
                            addr_infos.push(addr_info);
                        } else {
                            debug!(
                                "addr {:?} is not connectable and not an onion address",
                                addr_info.addr
                            );
                        }
                    }
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

**File:** network/src/peer_store/mod.rs (L34-35)
```rust
const ADDR_MAX_RETRIES: u32 = 3;
const ADDR_MAX_FAILURES: u32 = 10;
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

**File:** network/src/peer_store/peer_store_impl.rs (L201-212)
```rust
        let filter = |peer_addr: &AddrInfo| {
            filter(peer_addr)
                && extract_peer_id(&peer_addr.addr)
                    .map(|peer_id| !peers.contains_key(&peer_id))
                    .unwrap_or_default()
                && peer_addr
                    .connected(|t| t > addr_expired_ms && t <= now_ms.saturating_sub(DIAL_INTERVAL))
                && required_flags_filter(required_flags, Flags::from_bits_truncate(peer_addr.flags))
        };

        // get addrs that can attempt.
        self.addr_manager.fetch_random(count, filter)
```

**File:** network/src/peer_store/peer_store_impl.rs (L230-239)
```rust
        let filter = |peer_addr: &AddrInfo| {
            filter(peer_addr)
                && extract_peer_id(&peer_addr.addr)
                    .map(|peer_id| !peers.contains_key(&peer_id))
                    .unwrap_or_default()
                && !peer_addr.tried_in_last_minute(now_ms)
                && !peer_addr.connected(|t| t > addr_expired_ms)
        };

        self.addr_manager.fetch_random(count, filter)
```

**File:** network/src/peer_store/peer_store_impl.rs (L327-355)
```rust
    fn check_purge(&mut self) -> Result<()> {
        if self.addr_manager.count() < ADDR_COUNT_LIMIT {
            return Ok(());
        }

        // Evicting invalid data in the peer store is a relatively rare operation
        // There are certain cleanup strategies here:
        // 1. First evict the nodes that have reached the eviction condition
        // 2. If the first step is unsuccessful, enter the network segment grouping mode
        //  2.1. Group current data according to network segment
        //  2.2. Sort according to the amount of data in the same network segment
        //  2.3. In the network segment with more than 4 peer, randomly evict 2 peer

        let now_ms = ckb_systemtime::unix_time_as_millis();
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

**File:** network/src/services/outbound_peer.rs (L69-78)
```rust
        let attempt_peers = self.network_state.with_peer_store_mut(|peer_store| {
            let paddrs = peer_store.fetch_addrs_to_feeler(FEELER_CONNECTION_COUNT, filter);
            for paddr in paddrs.iter() {
                // mark addr as tried
                if let Some(paddr) = peer_store.mut_addr_manager().get_mut(&paddr.addr) {
                    paddr.mark_tried(now_ms);
                }
            }
            paddrs
        });
```
