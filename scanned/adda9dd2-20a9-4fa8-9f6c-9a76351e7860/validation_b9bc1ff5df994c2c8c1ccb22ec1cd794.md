Now I have all the information needed. Let me trace the full attack path carefully.

### Title
`HOLE_PUNCHING_INTERVAL` Deduplication Bypass via Unique `from` PeerIds Causes Unbounded `pending_delivered` Growth and Forced Outbound Bandwidth — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

The `respond_delivered` function in `ConnectionRequestProcess` enforces a 2-minute deduplication window keyed solely on `from_peer_id`. An attacker connected to a target node (where `self == to`) can bypass this window entirely by sending `ConnectionRequest` messages with a freshly generated unique `from` PeerId per message. Each message passes all rate-limit and deduplication checks, causing the target to send a `ConnectionRequestDelivered` response for every single message and insert a new entry into the `pending_delivered` HashMap, which grows unboundedly until the 5-minute cleanup timer fires.

---

### Finding Description

**Entry point:** Any peer connected to the target node via the P2P `HolePunching` protocol.

**Attack flow:**

**Step 1 — Session-level rate limiter (partial guard only)**

In `received()`, the first check is:

```rust
if self.rate_limiter.check_key(&(session_id, msg.item_id())).is_err()
``` [1](#0-0) 

This limiter is configured at 30 requests/second per `(session_id, message_type)`:

```rust
let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
let rate_limiter = RateLimiter::hashmap(quota);
``` [2](#0-1) 

This throttles the attack to **30 messages/second per session** but does not prevent it.

**Step 2 — `forward_rate_limiter` is bypassed**

Inside `execute()`, the second check is:

```rust
.check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
``` [3](#0-2) 

This limiter is keyed on `(from_peer_id, to_peer_id, msg_item_id)`. With a unique `from` PeerId per message, every check sees a fresh key and passes. **Bypassed.**

**Step 3 — `HOLE_PUNCHING_INTERVAL` check is bypassed**

Inside `respond_delivered()`:

```rust
if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
    let now = unix_time_as_millis();
    if now - t < HOLE_PUNCHING_INTERVAL {
        return StatusCode::Ignore ...
    }
}
``` [4](#0-3) 

The deduplication map is keyed only on `from_peer_id`. With a unique `from` PeerId per message, `pending_delivered.get(&from_peer_id)` always returns `None`. **Bypassed.**

**Step 4 — Response sent and HashMap entry inserted**

After the bypass, the target:
1. Looks up its public/observed addresses (up to `ADDRS_COUNT_LIMIT = 24`)
2. Builds and sends a `ConnectionRequestDelivered` message back to the attacker
3. Inserts `(from_peer_id, (remote_listens, now))` into `pending_delivered` [5](#0-4) 

**Step 5 — Cleanup is infrequent**

The `pending_delivered` HashMap is only cleaned up every 5 minutes (`CHECK_INTERVAL`), retaining entries newer than 5 minutes (`TIMEOUT`):

```rust
self.pending_delivered.retain(|_, (_, t)| (now - *t) < TIMEOUT);
``` [6](#0-5) 

At 30 inserts/second for 5 minutes, the HashMap accumulates **9,000 entries** before any cleanup occurs.

**Step 6 — Rate limiter resets on reconnect**

On disconnect, `retain_recent()` is called:

```rust
async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
    self.rate_limiter.retain_recent();
    self.forward_rate_limiter.retain_recent();
``` [7](#0-6) 

A reconnecting attacker gets a new `session_id`, so the 30/second cap resets. The `pending_delivered` HashMap is **not** cleared on disconnect, so entries from prior sessions accumulate.

---

### Impact Explanation

| Resource | Calculation | Per 5-minute window |
|---|---|---|
| `pending_delivered` entries | 30/s × 300s | 9,000 entries |
| Memory per entry | PeerId (~38B) + Vec<Multiaddr> (~1,200B) + u64 | ~1,240 bytes |
| Total memory | 9,000 × 1,240 | ~11 MB |
| Outbound bandwidth | 30 msg/s × ~1,200 bytes | ~36 KB/s per session |

The attacker forces the target to:
- Perform address lookups and message serialization for every request
- Send 30 `ConnectionRequestDelivered` messages/second
- Grow `pending_delivered` unboundedly within each 5-minute window
- Sustain the attack indefinitely by reconnecting (rate limiter resets; HashMap does not)

---

### Likelihood Explanation

The attack requires only a single P2P connection to the target node, which is a standard, unprivileged operation. PeerIds are freely generated (public keys). No PoW, no stake, no privileged access is required. The bypass is deterministic and requires no timing or race conditions.

---

### Recommendation

1. **Key `pending_delivered` on `(from_peer_id, sender_session_id)` or on the actual sending peer's verified identity**, not on the attacker-controlled `from` field in the message payload.
2. **Add a per-session cap on total `respond_delivered` calls** (e.g., max N responses per session lifetime), independent of `from` PeerId.
3. **Bound the size of `pending_delivered`** with an LRU or capacity limit to prevent unbounded memory growth.
4. **Clear `pending_delivered` entries associated with a disconnected session** in the `disconnected` handler.

---

### Proof of Concept

```
1. Connect to target CKB node T (where T's PeerId == `to`)
2. For i in 0..1000:
     from_i = generate_fresh_peer_id()  # unique per iteration
     msg = ConnectionRequest { from: from_i, to: T.peer_id, listen_addrs: [valid_tcp_addr], max_hops: 1, route: [] }
     send(msg)
     sleep(1/30)  # stay within session rate limit
3. Count ConnectionRequestDelivered messages received
4. Assert count == 1000  # not bounded by HOLE_PUNCHING_INTERVAL
5. Inspect target's pending_delivered HashMap size == 1000
```

The `HOLE_PUNCHING_INTERVAL` invariant is violated: the target sends one `ConnectionRequestDelivered` per unique `from`, not one per 2-minute window.

### Citations

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

**File:** network/src/protocols/hole_punching/mod.rs (L173-175)
```rust
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/mod.rs (L249-252)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (CHECK_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L161-167)
```rust
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
            }
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L226-237)
```rust
        if let Err(error) = self
            .p2p_control
            .send_message_to(self.peer, proto_id, new_message)
            .await
        {
            return StatusCode::ForwardError.with_context(error);
        }

        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```
