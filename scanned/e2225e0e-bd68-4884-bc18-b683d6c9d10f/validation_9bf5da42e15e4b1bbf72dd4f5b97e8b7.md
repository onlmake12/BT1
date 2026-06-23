Based on my investigation of the actual code, here is my assessment:

---

### Title
Unbounded `forward_rate_limiter` HashMapStateStore Growth via Unique (PeerId, PeerId) Pairs — (`network/src/protocols/hole_punching/mod.rs`)

### Summary

The `forward_rate_limiter` in `HolePunching` uses `governor::state::keyed::HashMapStateStore<(PeerId, PeerId, u32)>` and is only cleaned via `retain_recent()` on peer disconnect. An attacker maintaining a single long-lived session can send `ConnectionRequest` messages with unique `(from, to)` PeerId pairs at the per-session rate limit, inserting a new key into the store per message, with no eviction until disconnect.

### Finding Description

In `network/src/protocols/hole_punching/mod.rs`, the `HolePunching` struct holds two rate limiters:

```rust
rate_limiter: RateLimiter<(PeerIndex, u32)>,
forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
``` [1](#0-0) 

`retain_recent()` is called on **both** limiters only inside `disconnected()`:

```rust
async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
    self.rate_limiter.retain_recent();
    self.forward_rate_limiter.retain_recent();
    ...
}
``` [2](#0-1) 

The `notify()` timer fires every 5 minutes and cleans `pending_delivered` and `inflight_requests`, but **never calls `retain_recent()` on `forward_rate_limiter`**: [3](#0-2) 

In `received()`, the first guard is the per-session `rate_limiter` keyed by `(session_id, msg_item_id)`, capped at 30 req/sec: [4](#0-3) 

This allows up to 30 `ConnectionRequest` messages per second per session. Each message then hits `forward_rate_limiter.check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))`: [5](#0-4) 

Since `content.from` and `content.to` are attacker-controlled PeerIds parsed from the message body, each unique `(from, to)` pair inserts a **new key** into the `HashMapStateStore`. The `governor` crate does not evict stale entries automatically — that is the purpose of `retain_recent()`, which is never called during an active session.

### Impact Explanation

- Growth rate: 30 entries/sec × ~150 bytes/entry ≈ 4.3 KB/sec
- Over 24 hours: ~375 MB; over ~11 days on a 4 GB node: potential OOM crash
- Impact is local node crash (OOM), no remote code execution

### Likelihood Explanation

- Requires HolePunching to be enabled (opt-in via `support_protocols` config)
- Attacker needs only one persistent P2P connection — no special privileges
- Generating unique PeerIds is trivial (random bytes)
- Attack is slow: OOM takes days of continuous flooding, but the invariant is definitively broken
- The `notify` timer at 5-minute intervals is the natural fix point but is unused for this cleanup

### Recommendation

Call `self.forward_rate_limiter.retain_recent()` inside the `notify()` handler alongside the existing cleanup of `pending_delivered` and `inflight_requests`: [3](#0-2) 

### Proof of Concept

1. Enable HolePunching on a test node.
2. Connect one session and send `ConnectionRequest` messages at 30/sec, each with a freshly generated random `from` and `to` PeerId.
3. After N seconds, inspect `forward_rate_limiter` key count — it will equal N × 30 with no eviction.
4. `disconnected()` is never triggered; `notify()` fires every 5 minutes but does not call `retain_recent()`.
5. Memory grows linearly without bound for the session lifetime.

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

**File:** network/src/protocols/hole_punching/mod.rs (L169-175)
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
