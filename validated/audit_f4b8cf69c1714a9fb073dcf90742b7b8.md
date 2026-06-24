Audit Report

## Title
Unauthenticated Arbitrary Peer Forwarding via Attacker-Controlled `route` in `ConnectionRequestDelivered` — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

## Summary
`ConnectionRequestDeliveredProcess::execute()` forwards a `ConnectionRequestDelivered` message to whatever `PeerId` appears last in the attacker-supplied `route` field, with no verification that this route was established by a prior `ConnectionRequest`. The `forward_rate_limiter` key `(content.from, content.to, msg_item_id)` is entirely attacker-controlled, making it trivially bypassable by rotating `from`/`to` values. Rotating these values also causes unbounded growth of the relay's `forward_rate_limiter` hashmap, enabling memory exhaustion on the relay node.

## Finding Description
**Forwarding without state verification**: In `execute()`, after parsing the message, the only guards are a route-length check and the `forward_rate_limiter`. When `content.route` is non-empty, `content.route.last()` is passed directly to `forward_delivered()`, which looks up the peer in the live registry and sends the message unconditionally. [1](#0-0) 

The `inflight_requests` map is only consulted in the terminal branch (empty route, `self == from`), never during forwarding. [2](#0-1) 

**Rate limiter bypass**: The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)`. Both `content.from` and `content.to` are wire-supplied attacker-controlled fields. [3](#0-2) 

The `forward_rate_limiter` is a `HashMapStateStore`-backed governor limiter. [4](#0-3) 

By rotating `from`/`to` pairs, the attacker bypasses the 1 req/sec per-pair cap and simultaneously causes unbounded growth of the hashmap — one new entry per unique `(from, to)` pair. The hashmap is only pruned via `retain_recent()` on session disconnect. [5](#0-4) 

**Session-level cap is multiplicable**: The session-level limiter allows 30 req/sec per `(session_id, msg.item_id())`. [6](#0-5) 

Opening N sessions yields 30N req/sec of relay-forwarded messages to the target peer.

**`forward_delivered` pops the route**: The component-level `forward_delivered` function removes the last route element before forwarding. [7](#0-6) 

So the target peer P receives the message with an empty route, then attempts to forward to `content.from` (a random attacker-controlled ID not in P's registry), returning `Ignore`. NAT traversal is **not** triggered on P in this path (contrary to the claim), but P still incurs message parsing and registry lookup overhead per message.

## Impact Explanation
Two concrete impacts:

1. **Memory exhaustion on the relay node**: Each unique `(from, to)` pair the attacker sends creates a new entry in the relay's `forward_rate_limiter` hashmap. With no bound on the number of unique pairs and cleanup only on disconnect, an attacker can exhaust the relay's heap memory, crashing the node. This matches the allowed impact: *"Vulnerabilities which could easily crash a CKB node"* — **High (10001–15000 points)**.

2. **Relay as unconditional message forwarder / network congestion**: With N sessions × 30 req/sec, the attacker can flood any peer connected to the relay with unsolicited hole-punching messages, consuming relay CPU, relay-to-peer bandwidth, and target-peer processing. This matches: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"* — **High (10001–15000 points)**.

## Likelihood Explanation
Requires only a standard P2P connection to the relay node — no privilege, no key, no proof-of-work. The target peer's `PeerId` is discoverable via the discovery protocol. The attacker can open multiple sessions and rotate `from`/`to` freely. The attack is fully local-testable and requires no external coordination.

## Recommendation
1. **Verify sender identity**: Before forwarding, check that the actual session sender's `PeerId` equals `route.last()` — i.e., the message must have arrived from the peer it claims to be the next hop.
2. **Maintain relay forwarding state**: Store `(from, to)` pairs for `ConnectionRequest` messages this relay has forwarded, and reject `ConnectionRequestDelivered` messages whose `(from, to)` pair is not in that table.
3. **Bound the `forward_rate_limiter` hashmap**: Cap the number of tracked `(from, to)` pairs (e.g., via an LRU eviction policy) to prevent memory exhaustion from attacker-rotated pairs.
4. **Tie `from`/`to` to the authenticated session**: Use the session's verified `PeerId` as part of the rate-limiter key rather than the wire-supplied `content.from`.

## Proof of Concept
1. Attacker A establishes N P2P sessions to relay R using the HolePunching protocol.
2. A discovers that peer P (`PeerId = P_id`) is connected to R via the discovery protocol.
3. For each session, A sends in a loop (up to 30/sec per session):
   ```
   ConnectionRequestDelivered {
     from: random_id_i,   // rotated each message
     to:   random_id_j,   // rotated each message
     route: [P_id],
     sync_route: [],
     listen_addrs: [valid_multiaddr_with_P_id]
   }
   ```
4. R's `execute()` hits `content.route.last() == Some(P_id)`, calls `forward_delivered(P_id)`.
5. R looks up `P_id` in `peer_registry`, finds the session, and sends the message to P.
6. Each unique `(random_id_i, random_id_j)` pair inserts a new entry into R's `forward_rate_limiter` hashmap.
7. After sustained attack: R's heap grows proportionally to the number of unique pairs sent; P receives 30N messages/sec and performs registry lookups for each.
8. **Memory exhaustion test**: Run R with a memory limit (e.g., via `ulimit` or cgroup), send millions of unique `(from, to)` pairs, and observe OOM crash on R.

### Citations

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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L147-148)
```rust
        match content.route.last() {
            Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L160-176)
```rust
                    let request_start = self.protocol.inflight_requests.remove(&content.to);

                    match request_start {
                        Some(start) => {
                            let res = self.respond_sync(content.from).await;
                            if !res.is_ok() {
                                return res;
                            }
                            let now = unix_time_as_millis();
                            let ttl = now - start;

                            self.try_nat_traversal(ttl, content.listen_addrs);

                            Status::ok()
                        }
                        None => StatusCode::Ignore.with_context("the request is not in flight"),
                    }
```

**File:** network/src/protocols/hole_punching/mod.rs (L46-46)
```rust
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```

**File:** network/src/protocols/hole_punching/mod.rs (L66-69)
```rust
    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.rate_limiter.retain_recent();
        self.forward_rate_limiter.retain_recent();
        debug!("HolePunching.disconnected session={}", context.session.id);
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

**File:** network/src/protocols/hole_punching/component/mod.rs (L223-241)
```rust
pub(crate) fn forward_delivered(
    delivered: packed::ConnectionRequestDeliveredReader<'_>,
) -> packed::ConnectionRequestDelivered {
    let route = delivered.route();
    let message = delivered.to_entity();
    let new_route = if route.is_empty() {
        packed::BytesVec::new_builder().build()
    } else {
        packed::BytesVec::new_builder()
            .extend(
                message
                    .route()
                    .into_iter()
                    .take(route.len().saturating_sub(1)),
            )
            .build()
    };
    message.as_builder().route(new_route).build()
}
```
