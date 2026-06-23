### Title
Forward Rate Limiter Keyed on `(from, to, item_id)` Instead of `(from, to)`, Allowing 3× Bypass — (`network/src/protocols/hole_punching/mod.rs`, `connection_sync.rs`, `connection_request.rs`, `connection_request_delivered.rs`)

---

### Summary

The `forward_rate_limiter` in the hole-punching protocol is intended to limit forwarding of messages for the same `(from, to)` peer pair to 1 request per second. However, the rate limiter key includes the message type discriminant (`item_id`), creating three independent 1 req/sec buckets for the same `(from, to)` pair — one per union variant. An attacker with a single connected session can send all three message types with the same `from`/`to` fields, achieving 3× the intended forwarding rate.

---

### Finding Description

The `forward_rate_limiter` is declared as `RateLimiter<(PeerId, PeerId, u32)>`: [1](#0-0) 

The stated design intent is explicit in the comment: [2](#0-1) 

But all three processors call `check_key` with a 3-tuple that includes `self.msg_item_id`:

- `ConnectionRequestProcess::execute`: [3](#0-2) 
- `ConnectionRequestDeliveredProcess::execute`: [4](#0-3) 
- `ConnectionSyncProcess::execute`: [5](#0-4) 

The `item_id` values are fixed by the union variant — 0, 1, 2 respectively: [6](#0-5) 

Because the key is `(from, to, 0)`, `(from, to, 1)`, and `(from, to, 2)` — three distinct keys — each passes the 1 req/sec check independently. The intended key should be `(from, to)` only.

The per-session rate limiter (keyed by `(session_id, item_id)`, 30 req/sec) does not compensate for this: [7](#0-6) 

---

### Impact Explanation

The actual amplification is **bounded at 3×** (not unbounded as the question claims). For each `(from, to)` pair, a single attacker session can trigger 3 forwards/sec instead of 1. Each forward fans out to `sqrt(total_connections)` peers via gossip. The claim of "unbounded CPU/memory exhaustion" is overstated — the real impact is a 3× increase in forwarding work per `(from, to)` pair across the gossip graph, causing measurable but not catastrophic overhead on intermediate relay nodes.

---

### Likelihood Explanation

Any connected peer can exploit this. The `from` and `to` fields are extracted from message content and are not verified against the actual session identity, so an attacker can craft messages with arbitrary PeerIds. The attack requires only a single P2P connection and the ability to send valid molecule-encoded messages of all three types.

---

### Recommendation

Change the `forward_rate_limiter` key type from `(PeerId, PeerId, u32)` to `(PeerId, PeerId)` and remove `self.msg_item_id` from all three `check_key` calls. This enforces the stated invariant: at most 1 forward per `(from, to)` pair per second regardless of message type.

---

### Proof of Concept

For the same `(from=A, to=B)` pair within one second:
1. Send `ConnectionRequest` with `from=A, to=B` → `check_key((A, B, 0))` → **passes** (bucket 0 empty)
2. Send `ConnectionRequestDelivered` with `from=A, to=B` → `check_key((A, B, 1))` → **passes** (bucket 1 empty)
3. Send `ConnectionSync` with `from=A, to=B` → `check_key((A, B, 2))` → **passes** (bucket 2 empty)

All three trigger forwarding to `sqrt(N)` peers. A unit test asserting that three distinct `item_id` values for the same `(from, to)` each independently pass `check_key` at 1 req/sec would confirm 3× the intended rate.

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
