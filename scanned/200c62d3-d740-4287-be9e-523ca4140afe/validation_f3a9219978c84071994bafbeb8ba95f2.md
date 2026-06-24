Audit Report

## Title
Unauthenticated `from` Field Enables `pending_delivered` Cache Poisoning and `forward_rate_limiter` Bypass — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary

The CKB hole punching protocol parses `content.from` directly from the message payload without verifying it matches the actual P2P session sender (`self.peer`). This allows any connected peer to spoof an arbitrary `from` peer ID, enabling two concrete attacks: (1) poisoning the `pending_delivered` cache under a victim's peer ID to silently block their NAT traversal for up to 2 minutes per injection, and (2) bypassing the `forward_rate_limiter` by rotating spoofed `from` values, causing relay nodes to forward up to 30× more messages than intended with multi-hop amplification.

## Finding Description

**Root cause:** In `ConnectionRequestProcess::execute()`, `content.from` is parsed from the message payload at [1](#0-0)  and is never compared against `self.peer` (the authenticated `PeerIndex` of the actual session sender), which is available in the struct at [2](#0-1) .

**Attack path 1 — `pending_delivered` cache poisoning:**

The `forward_rate_limiter` check uses the unauthenticated `content.from` as part of its key: [3](#0-2) 

When `self_peer_id == &content.to`, `respond_delivered` is called with the attacker-controlled `from_peer_id`. It first checks the cache: [4](#0-3) 

Then unconditionally inserts under the spoofed peer ID: [5](#0-4) 

An attacker sends `ConnectionRequest{from=victim_peer_id, to=relay_peer_id, listen_addrs=[attacker_addrs]}`. The relay inserts `pending_delivered[victim_peer_id] = (attacker_addrs, now)`. Any subsequent legitimate `ConnectionRequest` from the real victim to the same relay within `HOLE_PUNCHING_INTERVAL` (2 minutes) is silently dropped with `StatusCode::Ignore`. [6](#0-5) 

**Attack path 2 — `forward_rate_limiter` bypass with multi-hop amplification:**

The `forward_rate_limiter` is keyed on `(content.from, content.to, msg_item_id)` and is limited to 1 req/sec per unique triple. [7](#0-6) 

By rotating `content.from` across requests, each message generates a unique key, bypassing the 1 req/sec forward limit entirely. The primary per-session rate limiter (keyed on `(session_id, msg.item_id())`) allows 30 req/sec, so the attacker can send 30 messages/sec, all of which bypass the `forward_rate_limiter`. Each relay forwards to `sqrt(total_peers)` downstream peers. With up to `MAX_HOPS = 6` hops, the amplification factor is `(sqrt(N))^6`. For N=100 connected peers, this is `10^6` messages per second from a single attacker session sending 30 req/sec — a ~33,000× amplification. The same bypass applies in `ConnectionSyncProcess` and `ConnectionRequestDeliveredProcess`. [8](#0-7) 

**Existing guards are insufficient:** The primary `rate_limiter` (30 req/sec per session) does not prevent the `forward_rate_limiter` bypass, since the bypass operates at the forwarding layer, not the ingress layer. The `remote_listens` filtering (TCP/IP4/IP6 only) is a minor constraint easily satisfied by the attacker.

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

The `forward_rate_limiter` bypass enables a single attacker with one P2P connection to generate exponentially amplified message floods across the relay network. The `pending_delivered` cache poisoning provides a targeted, repeatable DoS against any peer's NAT traversal capability, requiring only knowledge of the victim's peer ID (publicly available via the discovery protocol).

## Likelihood Explanation

The attack requires only a single authenticated P2P connection to any CKB node with the `HolePunching` protocol enabled. No special privileges, keys, or hash power are needed. Victim peer IDs are publicly advertised via the discovery protocol. The attacker can maintain the cache poisoning indefinitely by re-injecting every ~2 minutes, and the rate-limiter bypass is trivially automated by generating random `from` bytes per request.

## Recommendation

In `ConnectionRequestProcess::execute()`, resolve the actual sender's `PeerId` from the peer registry using `self.peer` and assert it equals `content.from` before proceeding:

```rust
let actual_sender_peer_id = self.protocol.network_state
    .with_peer_registry(|reg| reg.get_peer(self.peer).map(|p| p.peer_id.clone()));
if actual_sender_peer_id.as_ref() != Some(&content.from) {
    return StatusCode::InvalidFromPeerId.with_context("from does not match session sender");
}
```

Alternatively, replace `content.from` in all security-sensitive operations (`pending_delivered` insert, `forward_rate_limiter` key) with the session-derived peer ID. Apply the same fix to `ConnectionSyncProcess` and `ConnectionRequestDeliveredProcess`.

## Proof of Concept

1. Attacker `A` establishes a P2P connection to relay node `R` with the `HolePunching` protocol.
2. `A` learns victim `V`'s peer ID from the discovery protocol.
3. **Cache poisoning:** `A` sends `ConnectionRequest{from=V.peer_id, to=R.peer_id, listen_addrs=[A's TCP addr]}`. `R` calls `respond_delivered(V.peer_id, ...)` and inserts `pending_delivered[V.peer_id] = ([A's addr], now)`. When `V` sends a legitimate `ConnectionRequest{from=V.peer_id, to=R.peer_id}` within 2 minutes, `R` returns `StatusCode::Ignore`. `A` re-sends every ~2 minutes to maintain the block indefinitely.
4. **Rate-limiter bypass:** `A` sends 30 `ConnectionRequest` messages/sec (primary rate limit), each with a freshly generated random `from` peer ID. Each generates a unique `(from, to, item_id)` key, bypassing the 1 req/sec `forward_rate_limiter`. `R` forwards all 30 to `sqrt(N)` downstream peers per second. Each downstream relay repeats the forwarding for up to 6 hops, producing exponential amplification.

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L85-91)
```rust
pub(crate) struct ConnectionRequestProcess<'a> {
    message: packed::ConnectionRequestReader<'a>,
    protocol: &'a mut HolePunching,
    peer: PeerIndex,
    p2p_control: &'a ServiceAsyncControl,
    msg_item_id: u32,
}
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L132-143)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
        {
            debug!(
                "from: {}, to {}, item_name: {}, rate limit is reached",
                content.from, content.to, "ConnectionRequest",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionRequest");
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L161-167)
```rust
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
            }
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/mod.rs (L24-24)
```rust
pub(crate) const HOLE_PUNCHING_INTERVAL: u64 = 2 * 60 * 1000; // 2 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L85-96)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
        {
            debug!(
                "from: {}, to {}, item_name: {}, rate limit is reached",
                content.from, content.to, "ConnectionSync",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionSync");
        }
```
