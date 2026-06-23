Now I have all the information needed to analyze this vulnerability claim thoroughly.

### Title
Unbounded `pending_delivered` and `forward_rate_limiter` Heap Growth via Unique `from` Peer IDs in `ConnectionRequestProcess::execute` — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

An unprivileged remote peer can drive unbounded heap growth in the victim node's `HolePunching` protocol state by sending a stream of `ConnectionRequest` messages addressed `to=victim_peer_id` with a distinct randomly-generated `from` peer ID per message. Two independent deduplication guards — the `forward_rate_limiter` and the `pending_delivered` cooldown check — are both keyed exclusively on `from`, so each unique `from` creates a fresh bucket and bypasses both guards entirely. Entries accumulate in `pending_delivered` at the full outer rate-limit rate (30/sec per session) and are only evicted by `notify()` every 5 minutes. The `forward_rate_limiter`'s internal `HashMapStateStore` is never cleaned during an active session (only on `disconnected()`), making it a second, truly unbounded growth vector for long-lived connections.

---

### Finding Description

**Rate-limiter layer 1 — outer `rate_limiter`** is keyed by `(session_id, item_id)`: [1](#0-0) 

This correctly caps one session to 30 `ConnectionRequest` messages per second. It is not bypassed.

**Rate-limiter layer 2 — `forward_rate_limiter`** is keyed by `(from, to, msg_item_id)`: [2](#0-1) 

Because the key includes `content.from`, every message with a fresh random `from` peer ID creates a brand-new bucket in the `HashMapStateStore`. The 1-req/sec quota is never reached for any individual key, so the check always passes.

**Deduplication guard in `respond_delivered`** is also keyed by `from_peer_id`: [3](#0-2) 

Since each message carries a unique `from`, `pending_delivered.get(&from_peer_id)` always returns `None`. The 2-minute cooldown (`HOLE_PUNCHING_INTERVAL`) is never triggered.

**Unconditional insertion** follows every successful send: [4](#0-3) 

**Cleanup only in `notify()`**, which fires every 5 minutes: [5](#0-4) [6](#0-5) 

Entries with `(now - t) < TIMEOUT` (5 min) survive, so up to a full 5-minute window of insertions accumulates before any eviction.

**`forward_rate_limiter` is never cleaned during an active session.** `retain_recent()` is only called on `disconnected()`: [7](#0-6) 

For a long-lived attacker connection, the `HashMapStateStore` grows at 30 entries/sec indefinitely — a second, unbounded growth vector independent of the 5-minute cleanup window.

---

### Impact Explanation

**Per-session, per-5-minute window:**
- `pending_delivered`: up to 30 × 300 = **9,000 entries**. Each entry holds a `PeerId` key (~39 bytes) plus a `Vec<Multiaddr>` (attacker supplies ≥1 valid TCP address, ~50–100 bytes) plus a `u64` timestamp. Roughly **~1–2 MB per session per window**.

**`forward_rate_limiter` internal state (unbounded for long-lived connections):**
- Grows at 30 entries/sec per session, never cleaned until disconnect. Over 1 hour: ~108,000 entries ≈ **~10 MB per session**. Over 24 hours: **~240 MB per session**.

**With multiple inbound sessions** (typical `max_inbound` ~125):
- 125 sessions × 10 MB/hr = **~1.25 GB/hr** from the rate-limiter state alone, independent of `pending_delivered`.

This constitutes realistic, sustained heap growth that can exhaust available memory on a production node, causing an OOM crash, node restart, and loss of P2P connectivity — fragmenting the node from the network and causing it to miss blocks, which constitutes consensus deviation.

---

### Likelihood Explanation

The attack requires only:
1. A single TCP connection to the victim (unprivileged, no authentication).
2. Crafting `ConnectionRequest` messages with `to=victim_peer_id` (publicly known), a fresh random valid multihash `from` field per message, and at least one valid TCP `listen_addr` with an IPv4/IPv6 component.
3. Sustaining the connection for hours.

All three conditions are trivially achievable by any remote peer. No PoW, no key material, no privileged access is required.

---

### Recommendation

1. **Bound `pending_delivered` by size**, not just by time. Add a `MAX_PENDING_DELIVERED` constant (e.g., 1,024) and reject insertions when the map is full, or use an LRU eviction policy.
2. **Periodically clean `forward_rate_limiter`** inside `notify()` (call `self.forward_rate_limiter.retain_recent()` there, not only in `disconnected()`).
3. **Key the `forward_rate_limiter` on `(session_id, to, item_id)`** instead of `(from, to, item_id)`. The `from` field is attacker-controlled and unbounded; `session_id` is bounded by the number of active connections.
4. **Add a global cap on `pending_delivered`** entries per originating session, since the current design allows a single session to fill the map at the full 30/sec rate.

---

### Proof of Concept

```rust
// Pseudocode unit test
let mut protocol = HolePunching::new(network_state);
let session_id = PeerIndex::new(1);

for i in 0..9000 {
    let from = PeerId::random(); // unique per iteration
    let msg = build_connection_request(
        from,
        /*to=*/ victim_peer_id.clone(),
        vec!["/ip4/1.2.3.4/tcp/8115".parse().unwrap()],
    );
    // outer rate_limiter: 30/sec per (session_id, item_id=0) — passes for first 30
    // forward_rate_limiter: (from, to, 0) — always a new key, always passes
    // respond_delivered: pending_delivered.get(&from) == None — always passes
    ConnectionRequestProcess::new(msg, &mut protocol, session_id, &control, 0)
        .execute()
        .await;
}

assert_eq!(protocol.pending_delivered.len(), /* up to */ 9000);
// forward_rate_limiter internal state also has 9000 entries, never cleaned
```

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L25-25)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```
