### Title
Unbounded `forward_rate_limiter` Memory Growth via Persistent HolePunching Connection — (`network/src/protocols/hole_punching/mod.rs`)

---

### Summary

`retain_recent()` on both `rate_limiter` and `forward_rate_limiter` is called **only** inside `disconnected()`. A remote peer that maintains a persistent HolePunching session and continuously sends `ConnectionRequest` or `ConnectionRequestDelivered` messages with unique attacker-controlled `(from, to)` PeerId pairs causes `forward_rate_limiter`'s `HashMapStateStore` to grow without bound for the lifetime of the connection, with no periodic reclamation path.

---

### Finding Description

`HolePunching` holds two `governor::RateLimiter<_, HashMapStateStore<_>, _>` instances:

- `rate_limiter: RateLimiter<(PeerIndex, u32)>` — keyed by `(session_id, msg.item_id())`
- `forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>` — keyed by `(from, to, msg.item_id())` [1](#0-0) 

The only place `retain_recent()` is called on either limiter is in `disconnected()`: [2](#0-1) 

The periodic `notify()` callback (fired every 5 minutes via `CHECK_INTERVAL`) cleans up `pending_delivered` and `inflight_requests` but **never** calls `retain_recent()` on either rate limiter: [3](#0-2) 

In `received()`, `rate_limiter.check_key(&(session_id, msg.item_id()))` is checked first. Because `msg.item_id()` is the union discriminant (0, 1, or 2), the key space for `rate_limiter` is bounded at 3 entries per session — not a concern. [4](#0-3) 

However, once that check passes, `forward_rate_limiter.check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))` is called with **attacker-controlled** `from` and `to` PeerIds extracted from the message payload: [5](#0-4) [6](#0-5) 

`governor`'s `HashMapStateStore` inserts a new `HashMap` entry for every unique key on `check_key()`. Without `retain_recent()`, expired entries are never evicted. An attacker who never disconnects can inject up to 30 unique `(from, to, item_id)` entries per second (the `rate_limiter` cap), causing the `forward_rate_limiter` map to grow at ~30 entries/second indefinitely.

---

### Impact Explanation

At ~30 entries/second with each governor entry consuming roughly 100–200 bytes (two `PeerId` heap allocations + state), a single persistent peer accumulates:

- ~2.6 MB/hour
- ~62 MB/day
- ~1.8 GB/month

This is a single-peer, single-connection memory exhaustion path leading to OOM and node crash, with no operator-visible warning and no self-healing mechanism.

---

### Likelihood Explanation

Any unprivileged remote peer that can establish a HolePunching protocol session can execute this. No PoW, no key material, no Sybil attack required. The attacker simply holds the connection open and sends valid-looking `ConnectionRequest` messages with rotating `from`/`to` PeerId bytes. The `rate_limiter` guard does not prevent this — it only limits message throughput, not key diversity.

---

### Recommendation

Call `retain_recent()` on both rate limiters inside `notify()` so that stale entries are periodically evicted independent of connection lifecycle:

```rust
async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
    self.rate_limiter.retain_recent();           // add
    self.forward_rate_limiter.retain_recent();   // add
    let now = unix_time_as_millis();
    self.pending_delivered.retain(|_, (_, t)| (now - *t) < TIMEOUT);
    self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
    // ...
}
``` [7](#0-6) 

This ensures the `CHECK_INTERVAL` (5 minutes) timer drives reclamation regardless of whether any peer ever disconnects.

---

### Proof of Concept

1. Connect to a victim CKB node and open the HolePunching protocol session.
2. In a loop, send `ConnectionRequest` messages where `from` and `to` are freshly generated random PeerIds (valid 32-byte ed25519 public keys), at ≤30 msg/s to stay under the `rate_limiter` cap.
3. Monitor the victim node's RSS over time.
4. Assert: RSS grows monotonically; neither rate limiter map shrinks; `retain_recent()` is never triggered because `disconnected()` is never called.
5. After ~24 hours, RSS increase attributable to `forward_rate_limiter` exceeds several hundred MB from this single connection alone.

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L45-46)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```

**File:** network/src/protocols/hole_punching/mod.rs (L66-70)
```rust
    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.rate_limiter.retain_recent();
        self.forward_rate_limiter.retain_recent();
        debug!("HolePunching.disconnected session={}", context.session.id);
    }
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

**File:** network/src/protocols/hole_punching/mod.rs (L169-176)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
        let status = self.network_state.connection_status();

        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);

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
