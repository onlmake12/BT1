### Title
Unbounded `forward_rate_limiter` HashMapStateStore Growth via Spoofed `from` PeerId in ConnectionRequest — (`network/src/protocols/hole_punching/mod.rs`, `network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

The `HolePunching` protocol uses a two-layer rate limiter. The outer `rate_limiter` is keyed by `(session_id, item_id)` and caps at 30 messages/sec per session. The inner `forward_rate_limiter` is keyed by `(from: PeerId, to: PeerId, item_id)`. Because `from` is an attacker-controlled field in the message body with no validation against the actual sender, each of the 30 outer-allowed messages per second can carry a distinct `from`, creating a new bucket in `forward_rate_limiter`'s `HashMapStateStore`. The store is never cleaned up periodically — `retain_recent()` is called only in `disconnected()`. This allows a single unprivileged peer to grow the shared `forward_rate_limiter` HashMap at 30 entries/sec for the entire duration of the connection, with no bound.

---

### Finding Description

**Layer 1 — outer `rate_limiter`:**

The `received()` handler checks the outer limiter keyed by `(session_id, msg.item_id())` before dispatching: [1](#0-0) 

`ConnectionRequest` has `item_id() = 0`, so the outer limiter allows up to 30 `ConnectionRequest` messages per second from a single session.

**Layer 2 — inner `forward_rate_limiter`:**

Inside `ConnectionRequestProcess::execute()`, the inner limiter is checked using the attacker-supplied `content.from` field: [2](#0-1) 

The key is `(content.from, content.to, self.msg_item_id)`. The `from` field is parsed directly from the message bytes with no check that it matches the actual sending peer: [3](#0-2) 

Each unique `from` value creates a new entry in the `HashMapStateStore<(PeerId, PeerId, u32)>`.

**No periodic cleanup:**

`retain_recent()` is called only in `disconnected()`: [4](#0-3) 

The `notify()` callback fires every `CHECK_INTERVAL = 5 minutes` but only prunes `pending_delivered` and `inflight_requests` — it never calls `retain_recent()` on either rate limiter: [5](#0-4) 

**Shared state:**

`forward_rate_limiter` is a single instance on the `HolePunching` struct, shared across all sessions: [6](#0-5) 

Multiple attacker connections each contribute 30 entries/sec to the same HashMap.

---

### Impact Explanation

Each `(PeerId, PeerId, u32)` key in `HashMapStateStore` occupies approximately 150–200 bytes (two 39-byte multihash PeerIds + u32 + HashMap overhead + governor `AtomicU64` state). Growth rate and memory:

| Duration | Entries (1 session) | Memory (1 session) | Memory (50 sessions) |
|---|---|---|---|
| 5 min | 9,000 | ~1.8 MB | ~90 MB |
| 1 hour | 108,000 | ~21 MB | ~1.05 GB |
| 8 hours | 864,000 | ~173 MB | ~8.6 GB |

Although governor's `retain_recent()` would remove entries whose 1-second quota has replenished, it is never invoked during the connection lifetime. Entries from time T are eligible for removal at T+1 but remain in the HashMap until disconnect. A long-lived connection (or many concurrent attacker connections) causes unbounded memory growth, leading to OOM and node crash.

---

### Likelihood Explanation

- The attacker only needs a standard P2P connection — no special privileges, no PoW, no key material.
- Generating 30 valid `PeerId` values per second is trivial (any valid multihash bytes pass `PeerId::from_bytes`).
- The outer rate limiter is the only throttle, and it is exactly what enables the attack: it guarantees 30 new `forward_rate_limiter` entries per second per session.
- CKB nodes accept inbound connections from the public internet, making this reachable from any unprivileged peer.

---

### Recommendation

1. **Call `retain_recent()` periodically** inside the `notify()` callback (every `CHECK_INTERVAL`) on both `rate_limiter` and `forward_rate_limiter`, not only on disconnect.
2. **Validate `from` against the actual sender**: reject messages where `content.from` does not match the peer ID of the sending session. This eliminates the ability to create arbitrary new keys.
3. **Cap the `forward_rate_limiter` store size**: add a maximum entry count and evict oldest entries when the cap is reached.

---

### Proof of Concept

**Invariant test (pseudocode):**

```rust
// Setup: connect one attacker session
let mut protocol = HolePunching::new(network_state);
let session_id = PeerIndex::new(1);

// Attack: send 30 ConnectionRequest/sec for 300 seconds, each with unique `from`
for second in 0..300 {
    for i in 0..30 {
        let from = generate_unique_peer_id(second * 30 + i); // distinct each time
        let to   = generate_peer_id(0);
        let msg  = build_connection_request(from, to, max_hops=1);
        protocol.received(ctx_for(session_id), msg).await;
    }
    advance_clock_by(1_second);
}

// Assert: forward_rate_limiter has grown to ~9000 entries
// (retain_recent() was never called — no disconnect occurred)
assert!(
    forward_rate_limiter_len(&protocol) <= 30 * 1,  // FAILS: actual ~9000
    "invariant violated: forward_rate_limiter grew unboundedly"
);
```

The outer limiter passes all 30 messages/sec (quota = 30). Each unique `from` creates a new `HashMapStateStore` entry. After 300 seconds with no disconnect, `retain_recent()` has never been called, so all ~9,000 entries remain resident. With 50 concurrent attacker sessions, the store reaches ~450,000 entries (~90 MB) in 5 minutes and continues growing.

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L38-47)
```rust
pub(crate) struct HolePunching {
    network_state: Arc<NetworkState>,
    bind_addr: Option<SocketAddr>,
    // Request timestamp recorded
    inflight_requests: HashMap<PeerId, u64>,
    // Delivered timestamp recorded
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
}
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
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
