### Title
Unbounded `forward_rate_limiter` HashMap Growth via Attacker-Controlled `(from, to)` PeerIds Causes OOM — (`network/src/protocols/hole_punching/mod.rs`)

---

### Summary

`HolePunching::forward_rate_limiter` is a `governor::RateLimiter` backed by `HashMapStateStore<(PeerId, PeerId, u32)>`. Its keys are derived from attacker-controlled `from`/`to` fields in forwarded messages. `retain_recent()` is called only in `disconnected()`, never in the periodic `notify()` handler. A single long-lived session sending messages with unique `(from, to)` pairs causes the HashMap to grow without bound until the node OOMs.

---

### Finding Description

`forward_rate_limiter` is declared as: [1](#0-0) 

Its key type is `(PeerId, PeerId, u32)`. In `received()`, the outer `rate_limiter` is checked first, keyed by `(session_id, msg.item_id())`: [2](#0-1) 

This outer limiter is bounded: `session_id` is fixed per session and `msg.item_id()` is the union discriminant (only 3 values: `ConnectionRequest`, `ConnectionRequestDelivered`, `ConnectionSync`). It allows up to **30 messages/sec** per `(session, type)` through.

Each message that passes then calls `forward_rate_limiter.check_key(...)` with attacker-controlled content: [3](#0-2) 

`content.from` and `content.to` are arbitrary `PeerId` bytes from the message payload — no validation restricts them to known/connected peers. Each unique `(from, to, item_id)` tuple causes `HashMapStateStore` to insert a new entry. Since every key is unique, the rate limit is never triggered (each key has its own fresh bucket), so every message both passes and inserts.

`retain_recent()` is called **only** in `disconnected()`: [4](#0-3) 

The periodic `notify()` handler (fires every `CHECK_INTERVAL = 5 minutes`) cleans `pending_delivered` and `inflight_requests` but **never** calls `retain_recent()` on either rate limiter: [5](#0-4) 

---

### Impact Explanation

- **Growth rate**: 30 entries/sec × 3 message types = up to 90 new HashMap entries/sec from a single session.
- **Entry size**: Each key is two `PeerId` values (~39 bytes each) + `u32` + governor bucket state ≈ ~150–200 bytes.
- **Accumulation**: ~48 MB/hour, ~1.2 GB/day, ~8 GB/week — from a single attacker session.
- **Result**: Node process OOM-killed, causing a full denial of service.

---

### Likelihood Explanation

The attacker needs only a single unprivileged P2P connection with the HolePunching protocol negotiated. No PoW, no keys, no special role. The attack is trivially automatable: craft `ConnectionRequest` messages with random 32-byte `from`/`to` fields at 30 msg/sec and hold the session open. The outer rate limiter does not prevent this — it only throttles the rate, not the total unique-key count.

---

### Recommendation

Call `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside the `notify()` handler, which already fires every 5 minutes:

```rust
async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
    self.rate_limiter.retain_recent();           // add
    self.forward_rate_limiter.retain_recent();   // add
    // ... existing cleanup ...
}
```

This bounds the HashMap to entries active within the last rate-limit window, regardless of session duration.

---

### Proof of Concept

```python
# Pseudocode: single TCP session, send N ConnectionRequest messages
# each with a unique random (from_peer_id, to_peer_id)
import os, time
session = connect_to_ckb_node_hole_punching_protocol()
for _ in range(10_000_000):
    from_id = os.urandom(32)
    to_id   = os.urandom(32)
    msg = build_connection_request(from_id, to_id, max_hops=6, listen_addrs=[...])
    session.send(msg)
    time.sleep(1/30)  # stay within outer rate limit
# Monitor: node RSS grows ~150 bytes per iteration; no cleanup until disconnect
# Expected: node OOM-killed after sustained run (hours to days)
```

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
