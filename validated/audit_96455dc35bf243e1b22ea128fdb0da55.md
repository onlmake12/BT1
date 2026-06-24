All cited code references have been verified against the actual source. Here is the validation result:

Audit Report

## Title
Onion3 `is_connectable` Bypass in `fetch_random` Permanently Pollutes Feeler Connection Pool — (File: `network/src/peer_store/addr_manager.rs`)

## Summary
`fetch_random` unconditionally returns Onion3 addresses regardless of `is_connectable` status via a logical OR bypass. An attacker connected as a peer can relay fake Onion3 multiaddrs through P2P discovery, causing the feeler service to permanently exhaust its 10 connection slots on dead addresses, degrading peer discovery indefinitely without any ongoing attacker presence.

## Finding Description

**Root cause — `fetch_random` Onion3 bypass** (`addr_manager.rs` lines 74–90):

When `multiaddr_to_socketaddr` returns `None` (always the case for Onion3 addresses), the code uses a logical OR:

```rust
None => {
    if filter(&addr_info) {
        if addr_info.is_connectable(now_ms)
            || addr_info.addr.iter().any(|p| matches!(p, Protocol::Onion3(_)))
        {
            addr_infos.push(addr_info);
        }
    }
}
``` [1](#0-0) 

Any Onion3 address is returned regardless of `is_connectable`. The regular IP branch (lines 66–72) enforces `is_connectable` strictly. [2](#0-1) 

**`is_connectable` permanently rejects addresses** when `last_connected_at_ms == 0 && attempts_count >= ADDR_MAX_RETRIES` (3): [3](#0-2) 

**`add_addr`** (the P2P discovery ingress) creates `AddrInfo` with `last_connected_at_ms = 0` and `attempts_count = 0`: [4](#0-3) 

**`fetch_addrs_to_feeler` filter** does not check `is_connectable` — only `!tried_in_last_minute` and `!connected(within 3 days)`. Onion3 addresses with `last_connected_at_ms = 0` pass both conditions: [5](#0-4) 

**`dial_feeler`** calls `fetch_addrs_to_feeler` then immediately calls `mark_tried` on every returned address, incrementing `attempts_count`: [6](#0-5) 

**Complete self-sustaining attack cycle:**
1. Attacker (connected peer) relays N fake Onion3 multiaddrs via P2P discovery → `add_addr` stores them with `attempts_count = 0`, `last_connected_at_ms = 0`.
2. Feeler fires → `fetch_addrs_to_feeler` → `fetch_random` returns Onion3 addresses (pass feeler filter, pass Onion3 bypass) → `mark_tried` increments `attempts_count`.
3. After 3 feeler cycles (≥3 minutes), `attempts_count = 3`, `last_connected_at_ms = 0` → `is_connectable` returns `false`.
4. Next feeler cycle: `tried_in_last_minute` is false (>1 min elapsed) → feeler filter passes → Onion3 bypass in `fetch_random` passes → address returned again → `mark_tried` called again.
5. Loop continues indefinitely. All `FEELER_CONNECTION_COUNT = 10` slots are consumed by permanently dead Onion3 addresses. [7](#0-6) 

**Why `check_purge` does not mitigate this:** `check_purge` only runs when `add_addr` is called and the store reaches `ADDR_COUNT_LIMIT` (16384). An attacker injecting fewer than 16384 addresses never triggers eviction. Even when triggered, `check_purge` uses `is_connectable` directly (correctly identifying dead Onion3 addresses for removal), but the attacker can immediately re-inject them via discovery. [8](#0-7) 

**`fetch_addrs_to_attempt` is not affected** because it requires `last_connected_at_ms > addr_expired_ms`, which Onion3 addresses with `last_connected_at_ms = 0` cannot satisfy — only the feeler path is impacted. [9](#0-8) 

## Impact Explanation
The feeler service is the primary mechanism for validating and onboarding new peers into the peer store. With all 10 feeler slots permanently occupied by attacker-injected dead Onion3 addresses, the node cannot validate or discover new reachable peers. Over time, as existing connections drop, the node's ability to maintain a healthy outbound peer set degrades. This constitutes a **suboptimal implementation of the CKB state storage mechanism** (peer store address management), qualifying as **Medium (2001–10000 points)**. The node does not crash and consensus is not immediately affected, but peer discovery is durably impaired without any ongoing attacker presence.

## Likelihood Explanation
- Onion3 multiaddrs are valid multiaddr format and accepted by `add_addr` without special filtering.
- The P2P discovery protocol is unauthenticated and unprivileged — any connected peer can relay addresses.
- No Tor support is required on the victim node; the feeler will attempt to dial and fail, incrementing `attempts_count` regardless.
- The attack is effective against the default TCP transport (`TransportType::Tcp` filter returns `true` for all addresses including Onion3).
- The cycle is self-sustaining: no ongoing attacker presence is needed after the initial injection.
- The attacker needs only to inject addresses below the `ADDR_COUNT_LIMIT` (16384) threshold to avoid triggering `check_purge`.

## Recommendation
Remove the unconditional Onion3 bypass in `fetch_random`. Onion3 addresses that have exhausted retries should be treated identically to any other permanently non-connectable address:

```rust
None => {
    if filter(&addr_info) && addr_info.is_connectable(now_ms) {
        addr_infos.push(addr_info);
    } else {
        debug!(
            "addr {:?} is not connectable",
            addr_info.addr
        );
    }
}
```

If Onion3 addresses genuinely require special liveness semantics (e.g., intermittent Tor circuits), a separate bounded retry budget distinct from `ADDR_MAX_RETRIES` should be used, rather than bypassing `is_connectable` entirely.

## Proof of Concept

```rust
// Unit test — no Tor required
let mut addr_manager = AddrManager::default();
let now_ms = ckb_systemtime::unix_time_as_millis();

for i in 0..20u8 {
    let onion_addr: Multiaddr = format!(
        "/onion3/{}:{}/p2p/{}",
        "a".repeat(56), i, PeerId::random().to_base58()
    ).parse().unwrap();
    let mut info = AddrInfo::new(onion_addr, 0, 100, Flags::COMPATIBILITY.bits());
    // Simulate 3 failed feeler attempts (attempts_count >= ADDR_MAX_RETRIES, last_connected_at_ms == 0)
    info.attempts_count = 3;
    addr_manager.add(info);
}

let results = addr_manager.fetch_random(20, |_| true);
// All 20 are returned despite is_connectable() == false for every one
assert_eq!(results.len(), 20);
for r in &results {
    assert!(!r.is_connectable(now_ms)); // confirms the bypass
}
```

Manual steps to trigger in a live node:
1. Connect to a victim CKB node as a peer.
2. Via the P2P discovery protocol, advertise 20+ fake Onion3 multiaddrs.
3. Wait 3+ minutes (3 feeler cycles).
4. Observe via node metrics/logs that feeler dials are exclusively to Onion3 addresses and no new legitimate peers are being validated.

### Citations

**File:** network/src/peer_store/addr_manager.rs (L66-72)
```rust
                    if (is_test_ip || is_unique_ip)
                        && addr_info.is_connectable(now_ms)
                        && filter(&addr_info)
                    {
                        duplicate_ips.insert(ip);
                        addr_infos.push(addr_info);
                    }
```

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

**File:** network/src/peer_store/peer_store_impl.rs (L201-209)
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

**File:** network/src/services/outbound_peer.rs (L22-22)
```rust
const FEELER_CONNECTION_COUNT: usize = 10;
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
