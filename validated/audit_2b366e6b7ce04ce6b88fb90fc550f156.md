Audit Report

## Title
Off-by-one in route-length guard allows attacker to cause relay nodes to be banned by their peers — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary
In `ConnectionRequestProcess::execute()`, both validation guards use strict `>` instead of `>=`, allowing a crafted `ConnectionRequest` with `route.len() = MAX_HOPS = 6` and `max_hops = 6` to pass all checks. When Node A calls `forward_request`, it appends itself to the route (now length 7) and forwards the message. Node B receives a message with `route.len() = 7 > MAX_HOPS = 6`, returns `InvalidRoute` (status 413), and bans Node A for 24 hours. An attacker with a single P2P connection to Node A can systematically sever Node A's connections to all its forwarding peers.

## Finding Description
`MAX_HOPS = 6` is defined at `network/src/protocols/hole_punching/mod.rs` L23.

In `execute()` at `connection_request.rs` L120–125:
```rust
if content.max_hops > MAX_HOPS { ... }       // 6 > 6 = false → passes
if content.route.len() > MAX_HOPS as usize { ... }  // 6 > 6 = false → passes
```

Since `max_hops != 0` and `self_peer_id != content.to`, execution reaches `forward_message` → `forward_request` at `component/mod.rs` L171–187, which unconditionally pushes `current_id` onto the route and decrements `max_hops`:
```rust
let new_route = message.route().as_builder().push(current_id.as_bytes()).build();
message.as_builder().max_hops(max_hops.saturating_sub(1)).route(new_route).build()
```

The forwarded message now has `route.len() = 7`. Node B's identical check `7 > 6` is true, returning `StatusCode::InvalidRoute` (413). In `mod.rs` L145–155, Node B's `received` handler calls `status.should_ban()`, which returns `Some(BAD_MESSAGE_BAN_TIME)` for any 4xx code (`status.rs` L99–106), and bans Node A's session for 24 hours. The invariant `route.len() + 1 <= MAX_HOPS` is never enforced before forwarding.

## Impact Explanation
An attacker can cause Node A to be banned by every peer it would forward to, effectively isolating Node A from the P2P network. Repeated across multiple relay nodes, this fragments the P2P topology and degrades network connectivity. This matches the **High** impact class: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."

## Likelihood Explanation
The attacker requires only a single P2P connection to a relay node — no PoW, no keys, no privileged role. The crafted message is trivially constructable. The rate limiter at `connection_request.rs` L132–143 keys on `(from, to, item_id)`, all of which the attacker controls, making the attack freely repeatable with different tuples. The attack is deterministic and requires no timing or race conditions.

## Recommendation
Change the route-length guard to account for the pending append before forwarding:
```rust
// route will grow by 1 in forward_request; reject if it would exceed MAX_HOPS
if content.route.len() >= MAX_HOPS as usize {
    return StatusCode::InvalidRoute.with_context("route too long to forward");
}
```
This ensures the forwarded message's `route.len()` is at most `MAX_HOPS`, which passes the next hop's check.

## Proof of Concept
```rust
// Craft a ConnectionRequest with route.len() = MAX_HOPS and max_hops = MAX_HOPS
let route: Vec<PeerId> = (0..MAX_HOPS).map(|_| PeerId::random()).collect();
let msg = packed::ConnectionRequest::new_builder()
    .from(attacker_id.as_bytes())
    .to(target_id.as_bytes())
    .max_hops(MAX_HOPS)          // 6 > 6 = false → passes check
    .route(/* 6 peer IDs */)     // 6 > 6 = false → passes check
    .listen_addrs(/* valid */)
    .build();

// Send to Node A. Node A passes both guards and calls forward_request.
let forwarded = forward_request(msg.as_reader(), &relay_a_id);
// forwarded.route().len() == 7, forwarded.max_hops() == 5
assert!(forwarded.route().len() > MAX_HOPS as usize); // 7 > 6 — Node B will ban Node A
// Node B receives forwarded: route.len()=7 > MAX_HOPS=6 → InvalidRoute(413) → ban Node A 24h
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L23-23)
```rust
pub(crate) const MAX_HOPS: u8 = 6;
```

**File:** network/src/protocols/hole_punching/mod.rs (L145-155)
```rust
        if let Some(ban_time) = status.should_ban() {
            error!(
                "process {} from {}; ban {:?} since result is {}",
                item_name, session_id, ban_time, status
            );
            self.network_state.ban_session(
                &context.control().clone().into(),
                session_id,
                ban_time,
                status.to_string(),
            );
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L120-125)
```rust
        if content.max_hops > MAX_HOPS {
            return StatusCode::InvalidMaxTTL.into();
        }
        if content.route.len() > MAX_HOPS as usize {
            return StatusCode::InvalidRoute.with_context("the route length is too long");
        }
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L171-187)
```rust
pub(crate) fn forward_request(
    request: packed::ConnectionRequestReader<'_>,
    current_id: &PeerId,
) -> packed::ConnectionRequest {
    let max_hops: u8 = request.max_hops().into();
    let message = request.to_entity();
    let new_route = message
        .route()
        .as_builder()
        .push(current_id.as_bytes())
        .build();
    message
        .as_builder()
        .max_hops(max_hops.saturating_sub(1))
        .route(new_route)
        .build()
}
```

**File:** network/src/protocols/hole_punching/status.rs (L99-106)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        let code = self.code() as u16;
        if (400..500).contains(&code) {
            Some(BAD_MESSAGE_BAN_TIME)
        } else {
            None
        }
    }
```
