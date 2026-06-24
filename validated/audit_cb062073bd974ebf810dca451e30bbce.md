Audit Report

## Title
Unbounded `forward_rate_limiter` Hashmap Growth via Attacker-Rotated `(from, to)` Pairs Enables Memory Exhaustion, and Unauthenticated Route Forwarding Enables Peer Flooding — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

## Summary
`ConnectionRequestDeliveredProcess::execute()` forwards messages to any `PeerId` in the attacker-supplied `route` field without verifying a prior `ConnectionRequest` established that route. The `forward_rate_limiter` is keyed by the fully attacker-controlled triple `(content.from, content.to, msg_item_id)`, so rotating `from`/`to` values trivially bypasses the 1 req/sec per-pair cap and causes unbounded growth of the relay's `forward_rate_limiter` `HashMapStateStore`, which is only pruned on session disconnect. Together these enable relay memory exhaustion and unsolicited flooding of any connected peer.

## Finding Description

**Forwarding without state verification**: In `execute()`, after parsing and route-length checks, the only guard before forwarding is the `forward_rate_limiter`. When `content.route` is non-empty, `content.route.last()` is passed directly to `forward_delivered()`, which looks up the peer in the live registry and sends the message unconditionally. [1](#0-0) 

The `inflight_requests` map is consulted only in the terminal branch (empty route, `self == from`), never during forwarding. [2](#0-1) 

**Rate limiter bypass and unbounded hashmap growth**: The `forward_rate_limiter` is keyed by `(content.from, content.to, self.msg_item_id)`, where both `content.from` and `content.to` are wire-supplied attacker-controlled fields. [3](#0-2) 

The limiter uses a `HashMapStateStore` backend, meaning each unique key creates a new heap-allocated entry. [4](#0-3) 

`retain_recent()` on `forward_rate_limiter` is called only in `disconnected()`, never in the periodic `notify()` handler (which runs every 5 minutes and only prunes `pending_delivered` and `inflight_requests`). [5](#0-4) [6](#0-5) 

By rotating `from`/`to` pairs, the attacker bypasses the 1 req/sec per-pair cap and simultaneously inserts a new hashmap entry per unique pair. During a sustained attack (session kept open), the hashmap grows without bound.

**Session-level cap is multiplicable**: The outer `rate_limiter` allows 30 req/sec per `(session_id, msg.item_id())`. [7](#0-6) 

Opening N sessions yields 30N new `(from, to)` entries/sec in the hashmap and 30N forwarded messages/sec to the target peer.

**`forward_delivered` pops the route before forwarding**: The relay removes the last route element, so the target peer P receives the message with an empty route and attempts to forward to `content.from` (an attacker-controlled ID not in P's registry), returning `Ignore`. P still incurs message parsing and registry lookup overhead per message. [8](#0-7) 

## Impact Explanation

**Memory exhaustion on the relay node** (High, 10001–15000 points): Each unique `(from, to)` pair inserts a new entry into the relay's `forward_rate_limiter` hashmap. With no periodic pruning and cleanup only on disconnect, an attacker maintaining an open session can exhaust the relay's heap memory, crashing the node. This matches: *"Vulnerabilities which could easily crash a CKB node."*

**Peer flooding / network congestion** (High, 10001–15000 points): With N sessions × 30 req/sec, the attacker can flood any peer connected to the relay with unsolicited `ConnectionRequestDelivered` messages, consuming relay CPU, relay-to-peer bandwidth, and target-peer processing. This matches: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation

Requires only a standard P2P connection to the relay node — no privilege, no key, no proof-of-work. The target peer's `PeerId` is discoverable via the discovery protocol. The attacker can open multiple sessions and rotate `from`/`to` freely with no external coordination. The attack is fully local-testable and repeatable.

## Recommendation

1. **Verify sender identity before forwarding**: Check that the actual session sender's `PeerId` equals `route.last()` — the message must have arrived from the peer it claims to be the next hop.
2. **Maintain relay forwarding state**: Store `(from, to)` pairs for `ConnectionRequest` messages this relay has forwarded, and reject `ConnectionRequestDelivered` messages whose `(from, to)` pair is not in that table.
3. **Bound the `forward_rate_limiter` hashmap**: Cap the number of tracked `(from, to)` pairs via an LRU eviction policy to prevent memory exhaustion from attacker-rotated pairs.
4. **Periodically call `retain_recent()`**: Add `forward_rate_limiter.retain_recent()` to the `notify()` handler so stale entries are pruned on a schedule, not only on disconnect.
5. **Tie `from`/`to` to the authenticated session**: Use the session's verified `PeerId` as part of the rate-limiter key rather than the wire-supplied `content.from`.

## Proof of Concept

1. Attacker A establishes N P2P sessions to relay R using the HolePunching protocol.
2. A discovers that peer P (`PeerId = P_id`) is connected to R via the discovery protocol.
3. For each session, A sends in a loop (up to 30/sec per session):
   ```
   ConnectionRequestDelivered {
     from: random_id_i,   // unique per message
     to:   random_id_j,   // unique per message
     route: [P_id],
     sync_route: [],
     listen_addrs: [valid_multiaddr_with_P_id]
   }
   ```
4. R's `execute()` hits `content.route.last() == Some(P_id)`, calls `forward_delivered(P_id)`.
5. Each unique `(random_id_i, random_id_j)` pair inserts a new entry into R's `forward_rate_limiter` hashmap; the 1 req/sec cap is never hit because each pair is new.
6. R looks up `P_id` in `peer_registry` and sends the message to P at 30N msg/sec.
7. **Memory exhaustion test**: Run R with a memory limit (e.g., via `ulimit` or cgroup), send millions of unique `(from, to)` pairs from a single long-lived session, and observe OOM crash on R.
8. **Flooding test**: Confirm P receives 30N messages/sec and performs a registry lookup for each, measurable via P's CPU and network metrics.

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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L160-162)
```rust
                    let request_start = self.protocol.inflight_requests.remove(&content.to);

                    match request_start {
```

**File:** network/src/protocols/hole_punching/mod.rs (L31-46)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;

/// Hole Punching Protocol
pub(crate) struct HolePunching {
    network_state: Arc<NetworkState>,
    bind_addr: Option<SocketAddr>,
    // Request timestamp recorded
    inflight_requests: HashMap<PeerId, u64>,
    // Delivered timestamp recorded
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
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

**File:** network/src/protocols/hole_punching/mod.rs (L169-175)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
        let status = self.network_state.connection_status();

        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
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
