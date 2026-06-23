Now I have all the information needed. Let me analyze the complete picture.

**Key facts confirmed:**

1. `item_id()` is a **fixed constant per message type** — 0 for `ConnectionRequest`, 1 for `ConnectionRequestDelivered`, 2 for `ConnectionSync`. It is NOT attacker-controlled per-message. [1](#0-0) 

2. The outer `rate_limiter` is keyed by `(session_id, item_id)` — at most 3 entries per session (one per message type), bounded, not a problem. [2](#0-1) 

3. The `forward_rate_limiter` is keyed by `(PeerId, PeerId, u32)` where `from` and `to` are **attacker-controlled payload fields**, and `u32` is the fixed `item_id`. [3](#0-2) 

4. `retain_recent()` is called **only in `disconnected()`**, never during active sessions or in `notify()`. [4](#0-3) 

5. The `notify()` timer (every 5 minutes) cleans `pending_delivered` and `inflight_requests` but **does not call `retain_recent()` on either rate limiter**. [5](#0-4) 

6. Each distinct `(from, to)` pair in a message payload creates a new entry in `forward_rate_limiter`. The outer rate limiter caps throughput at 30/sec per message type, but does **not** cap the number of distinct keys. [6](#0-5) 

---

### Title
Unbounded `forward_rate_limiter` HashMapStateStore Growth via Attacker-Controlled `(from, to)` PeerId Pairs — (`network/src/protocols/hole_punching/mod.rs`)

### Summary
The `HolePunching` protocol's `forward_rate_limiter` uses a `governor::HashMapStateStore` keyed by `(PeerId, PeerId, u32)`. The `from` and `to` fields are attacker-controlled message payload bytes. `retain_recent()` is called only in `disconnected()`, never during active sessions or in the periodic `notify()` timer. An attacker maintaining a single long-lived session can continuously inject messages with distinct `(from, to)` pairs, causing the map to grow without bound.

### Finding Description
In `network/src/protocols/hole_punching/mod.rs`, `HolePunching` holds two rate limiters:

- `rate_limiter: RateLimiter<(PeerIndex, u32)>` — keyed by session ID + fixed item_id. Bounded (≤3 entries per session).
- `forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>` — keyed by payload-supplied `from`, `to`, and fixed item_id. **Unbounded key space.**

`disconnected()` calls `retain_recent()` on both, but this is never triggered during an active session. The `notify()` callback (every 5 minutes) only prunes `pending_delivered` and `inflight_requests`, not the rate limiters.

The outer `rate_limiter` allows 30 messages/sec per `(session_id, item_id)`. Each passing message with a fresh `(from, to)` pair inserts a new entry into `forward_rate_limiter`. Over a sustained session:

- 30 entries/sec × 3 message types = **90 new HashMap entries/sec**
- Each entry: `(PeerId ~39B, PeerId ~39B, u32 4B)` key + governor state + HashMap overhead ≈ ~140 bytes
- After 1 hour: ~45 MB; after 24 hours: ~1.1 GB; after 1 week: ~7.6 GB

### Impact Explanation
A single unprivileged peer maintaining a long-lived HolePunching session can cause monotonically increasing heap allocation on the victim node. No disconnect is needed. The node will eventually OOM-crash. The attack requires no special privileges, no PoW, and no cryptographic material.

### Likelihood Explanation
HolePunching is a production P2P protocol enabled via config. Any peer that can establish a TCP connection and negotiate the HolePunching protocol sub-stream can execute this. The attack is slow (hours to days to OOM a typical server), but it is persistent, requires no reconnection, and is not detectable by existing monitoring since the rate limiter silently accepts new keys.

### Recommendation
Call `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside the `notify()` callback, which already fires every 5 minutes. This is the same pattern used by the `Relayer` protocol. Alternatively, cap the maximum number of entries in the `forward_rate_limiter` by switching to a bounded LRU-keyed store or by rejecting messages whose `(from, to)` pair is not in a known/expected set.

### Proof of Concept
```
1. Connect to a CKB node with HolePunching enabled.
2. In a loop, send ConnectionRequest messages at 30/sec, each with a freshly generated random (from, to) PeerId pair and a valid listen_addr.
3. Never disconnect.
4. After N seconds, assert that the governor::HashMapStateStore internal entry count ≈ 30*N (grows linearly).
5. After ~3 hours, observe ~45 MB consumed by the forward_rate_limiter alone; after 24 hours, ~1.1 GB.
```

The root cause is at: [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** util/gen-types/src/generated/protocols.rs (L5548-5554)
```rust
    pub fn item_id(&self) -> molecule::Number {
        match self {
            HolePunchingMessageUnion::ConnectionRequest(_) => 0,
            HolePunchingMessageUnion::ConnectionRequestDelivered(_) => 1,
            HolePunchingMessageUnion::ConnectionSync(_) => 2,
        }
    }
```

**File:** network/src/protocols/hole_punching/mod.rs (L46-46)
```rust
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
