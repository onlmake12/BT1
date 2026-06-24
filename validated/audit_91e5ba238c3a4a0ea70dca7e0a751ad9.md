Audit Report

## Title
Unauthenticated `from` Field in Hole Punching Messages Enables `pending_delivered` Cache Poisoning and `forward_rate_limiter` Bypass — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary
`ConnectionRequestProcess::execute()`, `ConnectionRequestDeliveredProcess::execute()`, and `ConnectionSyncProcess::execute()` all parse `content.from` directly from the message payload without verifying it matches the authenticated session sender. This allows any connected peer to spoof an arbitrary `from` peer ID, enabling two concrete attacks: poisoning the `pending_delivered` cache to silently block a victim's NAT traversal for up to 2 minutes per injection, and bypassing the `forward_rate_limiter` by rotating spoofed `from` values to amplify message forwarding 30× beyond the intended 1/sec per flow.

## Finding Description

**Root cause:** In `ConnectionRequestProcess::execute()`, `content.from` is parsed from the message payload at [1](#0-0)  with no check that it matches the peer ID of the actual session sender. The struct holds `self.peer: PeerIndex` (the authenticated session ID) at [2](#0-1)  but it is never used to validate `content.from`.

**Attack 1 — `pending_delivered` cache poisoning:**

When `self_peer_id == &content.to`, `execute()` calls `respond_delivered(content.from, ...)` passing the attacker-controlled peer ID as the cache key: [3](#0-2) 

Inside `respond_delivered`, the relay checks whether an entry for `from_peer_id` already exists within `HOLE_PUNCHING_INTERVAL` (2 minutes) and silently drops the request if so: [4](#0-3) 

It then unconditionally writes the attacker-supplied listen addresses into the cache under the spoofed peer ID: [5](#0-4) 

An attacker sends `ConnectionRequest{from=victim_peer_id, to=relay_peer_id, listen_addrs=[attacker_addrs]}`. The relay inserts `pending_delivered[victim_peer_id] = (attacker_addrs, now)`. Any subsequent legitimate `ConnectionRequest` from the real victim to the same relay within 2 minutes is silently dropped. The attacker re-injects every ~2 minutes to maintain the block indefinitely.

When `ConnectionSync` arrives with `from=victim_peer_id`, the relay looks up `pending_delivered[victim_peer_id]` and attempts TCP hole punching to the attacker-controlled addresses: [6](#0-5) 

**Attack 2 — `forward_rate_limiter` bypass:**

The forward rate limiter is keyed on `(content.from, content.to, msg_item_id)` — all attacker-controlled: [7](#0-6) 

The limiter is configured at 1 request per second per key: [8](#0-7) 

By rotating `content.from` across requests, each message generates a unique key, so the `forward_rate_limiter` never triggers. The outer per-session `rate_limiter` (30 req/sec per `(session_id, msg_item_id)`) still applies: [9](#0-8)  but the attacker can now send all 30 messages/sec through the forward path instead of the intended 1/sec. Each forwarded message is broadcast to `sqrt(total_peers)` downstream nodes: [10](#0-9) 

The same unauthenticated `from` pattern exists in `ConnectionRequestDeliveredProcess`: [11](#0-10)  and `ConnectionSyncProcess`: [12](#0-11) 

## Impact Explanation

The rate-limiter bypass allows a single attacker session to inject 30 forwarded `ConnectionRequest` messages per second (30× the intended 1/sec per flow), each broadcast to `sqrt(N)` peers. With N=100 relay peers, this yields 300 message deliveries per second per attacker session, scaling with additional connections and larger peer counts. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** The cache poisoning provides a secondary targeted DoS: any peer behind NAT whose hole punching relies on a poisoned relay is silently blocked from NAT traversal indefinitely at negligible attacker cost.

## Likelihood Explanation

The attack requires only a single standard P2P connection to any CKB node with the `HolePunching` protocol enabled. No special privileges, keys, or hash power are needed. The victim's peer ID is publicly advertised via the discovery protocol. The attacker only needs to know the victim's peer ID and the relay's peer ID, both of which are publicly available. The attack is trivially repeatable and requires no victim interaction.

## Recommendation

In `ConnectionRequestProcess::execute()` (and analogously in `ConnectionRequestDeliveredProcess` and `ConnectionSyncProcess`), resolve the actual sender's `PeerId` from the peer registry using `self.peer` and assert it equals `content.from` before proceeding:

```rust
let actual_from = self.protocol.network_state
    .with_peer_registry(|reg| reg.get_peer(self.peer).map(|p| p.peer_id.clone()));
if actual_from.as_ref() != Some(&content.from) {
    return StatusCode::InvalidFromPeerId.with_context("from peer id does not match session sender");
}
```

Alternatively, replace `content.from` in all security-sensitive operations (`pending_delivered` insert/lookup, `forward_rate_limiter` key) with the session-derived peer ID, analogous to replacing a caller-supplied `from` address with `msg.sender` in Solidity. This eliminates the spoofing surface entirely.

## Proof of Concept

1. Attacker `A` establishes a standard P2P connection to relay node `R` with the `HolePunching` protocol.
2. `A` learns victim `V`'s peer ID from the discovery protocol.
3. `A` sends a well-formed `ConnectionRequest` message to `R` with `from=V.peer_id`, `to=R.peer_id`, `listen_addrs=[A's TCP address]`.
4. `R` processes the message: `self_peer_id == &content.to` is true, so it calls `respond_delivered(V.peer_id, R.peer_id, [A's addrs])`.
5. `R` finds no existing entry for `V.peer_id` in `pending_delivered`, sends `ConnectionRequestDelivered` back to `A`'s session, and inserts `pending_delivered[V.peer_id] = ([A's addrs], now)`.
6. `V` sends a legitimate `ConnectionRequest{from=V.peer_id, to=R.peer_id}`. `R` checks `pending_delivered.get(&V.peer_id)`, finds the entry from step 5, and since `now - t < HOLE_PUNCHING_INTERVAL` (2 min), returns `StatusCode::Ignore` — V's hole punching is silently blocked.
7. `A` re-sends the spoofed message every ~2 minutes to maintain the block indefinitely.
8. For the rate-limiter bypass: `A` sends 30 `ConnectionRequest` messages per second, each with a freshly generated random `from` peer ID and `to` set to some target peer. Each generates a unique `(from, to, item_id)` key, bypassing the `forward_rate_limiter`, causing `R` to forward all 30 per second to `sqrt(N)` downstream peers.

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L161-166)
```rust
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
            }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L280-305)
```rust
                let sid = self.peer;
                let mut total = self
                    .protocol
                    .network_state
                    .with_peer_registry(|p| p.peers().len())
                    .isqrt();
                if let Err(error) = self
                    .p2p_control
                    .filter_broadcast(
                        TargetSession::Filter(Box::new(move |id| {
                            if id == &sid {
                                return false;
                            }
                            total = total.saturating_sub(1);
                            total != 0
                        })),
                        proto_id,
                        new_message,
                    )
                    .await
                {
                    StatusCode::BroadcastError.with_context(error)
                } else {
                    Status::ok()
                }
            }
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-115)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());
```

**File:** network/src/protocols/hole_punching/mod.rs (L95-107)
```rust
        if self
            .rate_limiter
            .check_key(&(session_id, msg.item_id()))
            .is_err()
        {
            debug!(
                "process {} from {}; result is {}",
                item_name,
                session_id,
                status::StatusCode::TooManyRequests.with_context(msg.item_name())
            );
            return;
        }
```

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L134-145)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
        {
            debug!(
                "from: {}, to {}, item_name: {}, rate limit is reached",
                content.from, content.to, "ConnectionRequestDelivered",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionRequestDelivered");
        }
```
