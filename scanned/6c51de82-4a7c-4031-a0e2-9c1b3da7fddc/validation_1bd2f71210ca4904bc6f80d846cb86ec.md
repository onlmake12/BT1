### Title
Ping RTT Eviction Protection Uses `as_secs()` Integer Truncation, Collapsing All Sub-Second Peers to Equal Score — (`network/src/peer_registry.rs`)

### Summary
`try_evict_inbound_peer` compares peer ping RTTs using `.as_secs()`, which truncates to whole seconds. Every peer with a measured RTT between 1ms and 999ms maps to the integer `0`, making them indistinguishable from the fastest legitimate peers. The first `sort_then_drop` pass intended to protect the 8 lowest-latency peers instead protects an arbitrary 8 peers from the entire sub-second RTT pool, nullifying the ping-based protection tier.

### Finding Description

In `network/src/peer_registry.rs`, `try_evict_inbound_peer` runs three sequential `sort_then_drop` passes to protect peers before selecting an eviction candidate.

The first pass is meant to protect the `EVICTION_PROTECT_PEERS` (= 8) peers with the lowest ping: [1](#0-0) 

The comparator maps each peer's `ping_rtt` via `.as_secs()`: [2](#0-1) 

`Duration::as_secs()` returns the integer number of whole seconds, discarding all sub-second precision. Concretely:

| Actual RTT | `.as_secs()` result |
|---|---|
| 1 ms | 0 |
| 500 ms | 0 |
| 999 ms | 0 |
| 1000 ms | 1 |

All peers with RTT < 1 second receive the same sort key `0`. Rust's `sort_by` is not guaranteed to be stable in a way that preserves any meaningful ordering among equal elements here — the 8 "protected" slots are filled from an arbitrary subset of the entire sub-second RTT pool.

The same truncation defect also appears in the second `sort_then_drop` pass for `last_ping_protocol_message_received_at`: [3](#0-2) 

### Impact Explanation

An attacker who controls inbound connections and responds to pings within any sub-second window (e.g., 999 ms) is treated identically to a legitimate peer with 1 ms RTT. The ping-based protection tier — the first and primary behavioral signal that is "hard for an attacker to simulate" per the code comment at line 149 — provides no meaningful discrimination. The attacker's peers have an equal probability of occupying the 8 protected slots as the fastest legitimate peers, weakening the eclipse-resistance guarantee that the eviction logic is designed to provide. [4](#0-3) 

### Likelihood Explanation

Any attacker capable of establishing inbound connections and responding to ping messages within 999 ms (trivially achievable from any co-located or well-connected server) can exploit this. No special privileges, key material, or majority hashpower are required. The condition is reachable through the standard P2P inbound connection path. [5](#0-4) 

### Recommendation

Replace `.as_secs()` with a sub-second-precision comparison in both `sort_then_drop` comparators. The simplest correct fix is to compare `Duration` values directly (which implements `Ord`) or use `.as_millis()` / `.as_nanos()`:

```rust
// Ping RTT pass
let peer1_ping = peer1.ping_rtt.unwrap_or(Duration::MAX);
let peer2_ping = peer2.ping_rtt.unwrap_or(Duration::MAX);
peer2_ping.cmp(&peer1_ping)

// Last-message pass
let peer1_last = peer1.last_ping_protocol_message_received_at
    .map(|t| now.saturating_duration_since(t))
    .unwrap_or(Duration::MAX);
let peer2_last = peer2.last_ping_protocol_message_received_at
    .map(|t| now.saturating_duration_since(t))
    .unwrap_or(Duration::MAX);
peer2_last.cmp(&peer1_last)
```

### Proof of Concept

```rust
use std::time::Duration;

fn eviction_key(rtt: Duration) -> u64 {
    rtt.as_secs()  // current code
}

fn main() {
    let legitimate_peer_rtt = Duration::from_millis(1);
    let attacker_peer_rtt   = Duration::from_millis(999);

    assert_eq!(
        eviction_key(legitimate_peer_rtt),
        eviction_key(attacker_peer_rtt),
        "1ms and 999ms both map to 0 — attacker is indistinguishable from fastest peer"
    );
    // assertion passes: both return 0
}
```

This directly demonstrates that the sort comparator assigns equal rank to a 1 ms legitimate peer and a 999 ms attacker peer, confirming the protection is ineffective for the entire sub-second RTT range. [6](#0-5)

### Citations

**File:** network/src/peer_registry.rs (L17-17)
```rust
pub(crate) const EVICTION_PROTECT_PEERS: usize = 8;
```

**File:** network/src/peer_registry.rs (L115-121)
```rust
            if session_type.is_inbound() {
                if connection_status.non_whitelist_inbound >= self.max_inbound {
                    if let Some(evicted_session) = self.try_evict_inbound_peer(peer_store) {
                        evicted_peer = self.remove_peer(evicted_session);
                    } else {
                        return Err(PeerError::ReachMaxInboundLimit.into());
                    }
```

**File:** network/src/peer_registry.rs (L149-165)
```rust
        // Protect peers based on characteristics that an attacker hard to simulate or manipulate
        // Protect peers which has the lowest ping
        sort_then_drop(
            &mut candidate_peers,
            EVICTION_PROTECT_PEERS,
            |peer1, peer2| {
                let peer1_ping = peer1
                    .ping_rtt
                    .map(|p| p.as_secs())
                    .unwrap_or_else(|| u64::MAX);
                let peer2_ping = peer2
                    .ping_rtt
                    .map(|p| p.as_secs())
                    .unwrap_or_else(|| u64::MAX);
                peer2_ping.cmp(&peer1_ping)
            },
        );
```

**File:** network/src/peer_registry.rs (L173-176)
```rust
                let peer1_last_message = peer1
                    .last_ping_protocol_message_received_at
                    .map(|t| now.saturating_duration_since(t).as_secs())
                    .unwrap_or_else(|| u64::MAX);
```
