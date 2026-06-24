Audit Report

## Title
Forward Rate Limiter Keyed on `(from, to, item_id)` Instead of `(from, to)` Allows 3× Forwarding Rate Bypass — (`network/src/protocols/hole_punching/mod.rs`)

## Summary
The `forward_rate_limiter` in `HolePunching` is declared as `RateLimiter<(PeerId, PeerId, u32)>` and is intended to limit forwarding for the same `(from, to)` peer pair to 1 request per second. Because the key includes `msg_item_id` (the union variant discriminant), three independent 1 req/sec buckets exist per `(from, to)` pair — one per message type. An attacker with a single P2P connection can send all three message types with the same `from`/`to` fields within one second, achieving 3× the intended forwarding rate.

## Finding Description
The `forward_rate_limiter` is declared with a 3-tuple key type: [1](#0-0) 

The constructor comment makes the design intent explicit — the same `(from, to)` group should not be forwarded more than once per second: [2](#0-1) 

However, all three message processors call `check_key` with `self.msg_item_id` included in the key, creating three independent rate-limit buckets per `(from, to)` pair:

- `ConnectionRequestProcess::execute` (item_id = 0): [3](#0-2) 

- `ConnectionRequestDeliveredProcess::execute` (item_id = 1): [4](#0-3) 

- `ConnectionSyncProcess::execute` (item_id = 2): [5](#0-4) 

Because the keys are `(A, B, 0)`, `(A, B, 1)`, and `(A, B, 2)` — three distinct hash map entries — each passes the 1 req/sec check independently. The per-session rate limiter (keyed by `(session_id, item_id)` at 30 req/sec) does not compensate for this: [6](#0-5) 

The `from` and `to` fields are extracted from message content and are not verified against the actual session identity, so an attacker can craft messages with arbitrary `PeerId` values: [7](#0-6) 

Notably, `ConnectionRequest` triggers a gossip broadcast to `sqrt(N)` peers when the target is not directly connected: [8](#0-7) 

`ConnectionRequestDelivered` and `ConnectionSync` perform unicast forwarding rather than gossip broadcast, so the primary amplification vector is `ConnectionRequest`. The bug still allows 3× the intended total forwarding rate per `(from, to)` pair (1 gossip broadcast + 2 unicast forwards per second instead of 1 total forward per second).

## Impact Explanation
This matches the **High** impact category: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs." A single connected attacker can send 3 distinct message types per second for the same `(from, to)` pair, each passing the forward rate limiter independently. The `ConnectionRequest` type triggers a gossip broadcast to `sqrt(total_connections)` peers. By cycling through many distinct `(from, to)` pairs (bounded only by the 30 req/sec per-session limiter per message type), the attacker amplifies forwarding load across all relay nodes with minimal cost.

## Likelihood Explanation
Any peer that can establish a single P2P connection can exploit this. No special privileges are required. The `from` and `to` fields are not authenticated against the session, so the attacker can use arbitrary `PeerId` values to maximize distinct rate-limit buckets. The attack is repeatable indefinitely and requires only the ability to send valid molecule-encoded messages of all three hole-punching types.

## Recommendation
Change the `forward_rate_limiter` key type from `RateLimiter<(PeerId, PeerId, u32)>` to `RateLimiter<(PeerId, PeerId)>` in `mod.rs`, and remove `self.msg_item_id` from all three `check_key` calls in `connection_request.rs`, `connection_request_delivered.rs`, and `connection_sync.rs`. This enforces the stated invariant: at most 1 forward per `(from, to)` pair per second regardless of message type.

## Proof of Concept
For the same `(from=A, to=B)` pair within one second:
1. Send `ConnectionRequest` with `from=A, to=B` → `check_key((A, B, 0))` → **passes** (bucket 0 empty)
2. Send `ConnectionRequestDelivered` with `from=A, to=B` → `check_key((A, B, 1))` → **passes** (bucket 1 empty)
3. Send `ConnectionSync` with `from=A, to=B` → `check_key((A, B, 2))` → **passes** (bucket 2 empty)

All three trigger forwarding. A unit test constructing a `HolePunching` instance, calling `forward_rate_limiter.check_key` three times with the same `(A, B)` but different `item_id` values (0, 1, 2), and asserting all three return `Ok(())` within the same second would confirm the 3× bypass. With the correct fix (`(A, B)` key), only the first call would return `Ok(())`; the second and third would return `Err(NotUntil(...))`.

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L46-46)
```rust
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-40)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
            .map_err(|_| StatusCode::InvalidToPeerId.with_context("the to peer id is invalid"))?;
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L132-136)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L279-299)
```rust
                // Broadcast to a number of nodes equal to the square root of the total connection count using gossip.
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
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L134-138)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L85-89)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
```
