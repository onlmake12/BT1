The code is fully readable. Here is the complete analysis:

---

### Title
Unbounded `forward_rate_limiter` HashMap Growth via Spoofed `from` PeerId in `ConnectionRequest` — (`network/src/protocols/hole_punching/component/connection_request.rs`)

### Summary

The `forward_rate_limiter` in `HolePunching` uses a `HashMapStateStore` keyed by `(PeerId, PeerId, u32)`. The `from` field of a `ConnectionRequest` message is attacker-controlled and is never validated against the actual sender's peer ID. Each unique `(from, to, msg_item_id)` tuple creates a new bucket in the HashMap. `retain_recent()` is called **only** in `disconnected`, never periodically. A single connected peer can inject up to 30 unique `from` values per second (bounded by the outer `rate_limiter`), causing the HashMap to grow without bound for the lifetime of the connection.

### Finding Description

`forward_rate_limiter` is declared as:

```rust
forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
``` [1](#0-0) 

backed by `governor::state::keyed::HashMapStateStore<(PeerId, PeerId, u32)>`:

```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;
``` [2](#0-1) 

In `ConnectionRequestProcess::execute`, the key inserted into this map is:

```rust
.check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
``` [3](#0-2) 

`content.from` is parsed from the wire message's `from` bytes field. The `try_from` implementation validates only that the bytes decode as a syntactically valid `PeerId`:

```rust
let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
    StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
})?;
``` [4](#0-3) 

There is **no check** that `content.from` matches the actual sender's peer ID. An attacker can set `from` to any valid PeerId bytes.

`retain_recent()` is called **only** in `disconnected`:

```rust
async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
    self.rate_limiter.retain_recent();
    self.forward_rate_limiter.retain_recent();
``` [5](#0-4) 

The `notify` callback (fired every 5 minutes) cleans up `pending_delivered` and `inflight_requests` but **never** calls `retain_recent()` on `forward_rate_limiter`:

```rust
self.pending_delivered.retain(|_, (_, t)| (now - *t) < TIMEOUT);
self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
``` [6](#0-5) 

The outer `rate_limiter` (keyed by `(session_id, msg_item_id)`) limits to 30 `ConnectionRequest` messages per second per session:

```rust
let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
let rate_limiter = RateLimiter::hashmap(quota);
``` [7](#0-6) 

This is the only throttle. Each of those 30 messages per second can carry a distinct `from` PeerId, creating 30 new HashMap buckets per second per connection, indefinitely, as long as the attacker stays connected.

### Impact Explanation

Each `HashMapStateStore` entry holds two `PeerId` values (~39 bytes each), a `u32`, and governor's internal rate state (~40 bytes), totalling roughly 150–300 bytes per entry. At 30 entries/sec per connection, with a typical max of ~125 connections all controlled by the attacker, the growth rate is ~3,750 entries/sec ≈ ~1 MB/sec. A relay node with 4 GB of available heap would be exhausted in under an hour of sustained attack, causing an OOM crash and severing all relayed connections (network partition for peers behind this relay).

### Likelihood Explanation

The attack requires only a single persistent P2P connection. The attacker does not need any special privilege, PoW, or key material — only the ability to open a TCP connection to the target node and send well-formed `ConnectionRequest` messages with varying `from` bytes. The outer rate limiter (30/sec) is the only throttle and it is not a meaningful defense against a long-lived connection. The `from` field is completely under attacker control with no sender-identity binding.

### Recommendation

1. **Periodic cleanup**: Call `self.forward_rate_limiter.retain_recent()` inside the `notify` callback (every 5 minutes) alongside the existing `pending_delivered` and `inflight_requests` cleanup.
2. **Bind `from` to the actual sender**: Validate that `content.from` equals the peer ID of the actual sending session before inserting into `forward_rate_limiter`. This eliminates the spoofing vector entirely.
3. **Cap the HashMap size**: Add a hard upper bound on the number of entries in `forward_rate_limiter` and drop/reject messages when the cap is reached.

### Proof of Concept

```rust
// Pseudocode unit test
let mut protocol = HolePunching::new(network_state);
for i in 0..N {
    let fake_from = PeerId::random(); // distinct each iteration
    let msg = build_connection_request(fake_from, some_to, valid_listen_addr);
    protocol.received(ctx, msg).await;
}
// forward_rate_limiter internal map now has N entries
assert_eq!(forward_rate_limiter_size(&protocol), N);
// No disconnect occurred, so retain_recent() was never called
```

Each iteration passes the outer `rate_limiter` (keyed by `(session_id, 0)`) up to 30 times per second, and each unique `fake_from` creates a new bucket in `forward_rate_limiter`. The map grows to `N` without any eviction.

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L31-35)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;
```

**File:** network/src/protocols/hole_punching/mod.rs (L46-46)
```rust
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```

**File:** network/src/protocols/hole_punching/mod.rs (L66-68)
```rust
    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.rate_limiter.retain_recent();
        self.forward_rate_limiter.retain_recent();
```

**File:** network/src/protocols/hole_punching/mod.rs (L173-175)
```rust
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/mod.rs (L251-252)
```rust
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
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
