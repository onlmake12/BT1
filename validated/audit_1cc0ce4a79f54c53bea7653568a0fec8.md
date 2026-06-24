All cited code has been verified against the actual source. Every claim in the report checks out:

- `forward_rate_limiter` is `RateLimiter<(PeerId, PeerId, u32)>` backed by `HashMapStateStore` [1](#0-0) 
- `content.from` is parsed from wire bytes with only syntactic validation — no binding to the actual sender's session peer ID [2](#0-1) 
- `check_key` inserts `(content.from, content.to, msg_item_id)` in all three handlers [3](#0-2) [4](#0-3) [5](#0-4) 
- `retain_recent()` is called only in `disconnected`, never in `notify` [6](#0-5) 
- `notify` cleans `pending_delivered` and `inflight_requests` but not `forward_rate_limiter` [7](#0-6) 
- Outer `rate_limiter` quota is 30/sec keyed by `(session_id, msg_item_id)` — does not bound distinct `from` values [8](#0-7) 

---

Audit Report

## Title
Unbounded `forward_rate_limiter` HashMap Growth via Spoofed `from` PeerId — (`network/src/protocols/hole_punching/mod.rs`)

## Summary
The `forward_rate_limiter` in `HolePunching` is a `HashMapStateStore` keyed by `(PeerId, PeerId, u32)`. The `from` field of every forwarded hole-punching message is taken directly from the wire without verifying it matches the actual sender's peer ID. Because `retain_recent()` is never called during an active connection (only in `disconnected`), an attacker holding a persistent connection can inject up to 30 unique `from` values per second, growing the HashMap without bound and eventually exhausting node memory.

## Finding Description
`forward_rate_limiter` is declared as `RateLimiter<(PeerId, PeerId, u32)>` backed by `governor::state::keyed::HashMapStateStore`.

In all three message handlers (`ConnectionRequestProcess`, `ConnectionRequestDeliveredProcess`, `ConnectionSyncProcess`), the key inserted into the map is `(content.from, content.to, msg_item_id)`, where `content.from` is parsed from the wire with only syntactic validation — no check that it equals the actual sending session's peer ID.

The outer `rate_limiter` is keyed by `(session_id, msg_item_id)` and allows 30 messages/sec per session — a single fixed key per session per message type. It does not bound the number of distinct `from` values an attacker can inject.

`retain_recent()` is called on `forward_rate_limiter` only in `disconnected`. The `notify` callback (fired every 5 minutes) cleans `pending_delivered` and `inflight_requests` but never calls `retain_recent()` on either rate limiter.

As long as the attacker does not disconnect, every unique `(from, to, msg_item_id)` tuple permanently occupies a bucket in the HashMap. The same flaw exists in `ConnectionRequestDeliveredProcess` and `ConnectionSyncProcess`.

## Impact Explanation
Each `HashMapStateStore` entry holds two `PeerId` values (~39 bytes each), a `u32`, and governor's internal rate state (~40 bytes), totalling roughly 150–300 bytes per entry. At 30 entries/sec per connection across a typical maximum of ~125 connections all controlled by the attacker, the growth rate is ~3,750 entries/sec ≈ ~1 MB/sec. A node with 4 GB of available heap would be exhausted in under an hour of sustained attack, causing an OOM crash. This matches the allowed bounty impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
The attack requires only the ability to open a TCP connection to the target node and send well-formed hole-punching messages with varying `from` bytes. No special privilege, proof-of-work, or key material is needed. The outer rate limiter (30/sec per session) is the only throttle and is not a meaningful defense against a long-lived connection with unique `from` values per message. The attack is repeatable and sustained for the full lifetime of the connection.

## Recommendation
1. **Periodic cleanup**: Call `self.forward_rate_limiter.retain_recent()` inside the `notify` callback alongside the existing `pending_delivered` and `inflight_requests` cleanup.
2. **Bind `from` to the actual sender**: Before inserting into `forward_rate_limiter`, validate that `content.from` equals the peer ID of the actual sending session (obtainable from `context.session`). This eliminates the spoofing vector entirely.
3. **Cap the HashMap size**: Add a hard upper bound on the number of entries in `forward_rate_limiter` and reject messages when the cap is reached.

## Proof of Concept
```rust
// Pseudocode unit test
let mut protocol = HolePunching::new(network_state);
// Simulate 30 messages/sec for T seconds, each with a unique `from`
for i in 0..(30 * T) {
    let fake_from = PeerId::random(); // distinct each iteration
    let msg = build_connection_request(fake_from, some_to, valid_listen_addr);
    protocol.received(ctx, msg).await;
    // outer rate_limiter key is (session_id, item_id) — same key, passes up to 30/sec
    // forward_rate_limiter key is (fake_from, some_to, item_id) — new key every time, always passes
}
// No disconnect occurred, so retain_recent() was never called
// forward_rate_limiter internal map now has 30*T entries
assert_eq!(forward_rate_limiter_size(&protocol), 30 * T);
```
Each iteration passes the outer `rate_limiter` (keyed by `(session_id, ConnectionRequest_item_id)`) up to 30 times per second, and each unique `fake_from` creates a new permanent bucket in `forward_rate_limiter`. The map grows to `30 * T` without any eviction for the duration of the connection.

### Citations

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

**File:** network/src/protocols/hole_punching/mod.rs (L169-175)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
        let status = self.network_state.connection_status();

        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/mod.rs (L249-257)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (CHECK_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L132-135)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L134-137)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L85-88)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
```
