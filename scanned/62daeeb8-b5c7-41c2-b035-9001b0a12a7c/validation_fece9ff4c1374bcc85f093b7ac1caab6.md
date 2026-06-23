### Title
Unbounded `pending_delivered` HashMap Growth via Distinct `from` PeerIds in `ConnectionRequest` — (`network/src/protocols/hole_punching/component/connection_request.rs`)

### Summary

The `pending_delivered` HashMap in `HolePunching` has no size cap. An attacker with one or more connected sessions can insert an unbounded number of entries by sending `ConnectionRequest` messages with distinct `from` peer IDs targeting the local node as `to`. The only effective throttle is the outer per-session rate limiter (30 req/sec); the per-(from,to) `forward_rate_limiter` creates a new bucket per distinct `from` ID and never blocks the first request. Cleanup runs only every 5 minutes. The `forward_rate_limiter`'s internal `HashMapStateStore` also grows unboundedly while the session remains connected.

---

### Finding Description

**`pending_delivered` has no size bound.**

The map is declared as a plain `HashMap<PeerId, PendingDeliveredInfo>` with no capacity limit: [1](#0-0) 

Insertion happens unconditionally for any new `from_peer_id`: [2](#0-1) 

**The `HOLE_PUNCHING_INTERVAL` guard is per-`from_peer_id` only.**

The deduplication check at lines 161–167 only fires when the *same* `from_peer_id` is seen again within 2 minutes. A distinct `from` ID always bypasses it: [3](#0-2) 

**The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)`.**

Each new `from` ID creates a fresh bucket; the first request always passes. This limiter does not bound the number of distinct `from` IDs processed: [4](#0-3) 

**The only effective throttle is the outer per-session rate limiter (30/sec).** [5](#0-4) 

**Cleanup runs only every 5 minutes.** [6](#0-5) 

**The `forward_rate_limiter` internal state also grows unboundedly.** `retain_recent()` is only called on session disconnect: [7](#0-6) 

---

### Impact Explanation

With one session at 30 req/sec over a 5-minute window: **9,000 entries × up to 24 `Multiaddr` objects each** accumulate in `pending_delivered`. Each `Multiaddr` for a TCP/IP+PeerId address is ~60 bytes, yielding ~13 MB per session per 5-minute cycle. With N concurrent sessions the growth is linear: 10 sessions → ~130 MB, 50 sessions → ~650 MB. The `forward_rate_limiter`'s `HashMapStateStore` adds a parallel unbounded allocation of the same cardinality. Sustained attack across multiple cycles (entries inserted just after a cleanup survive until the next one, up to ~10 minutes of accumulation) doubles the worst-case figure. On memory-constrained validators this causes OOM, dropping the node from consensus.

---

### Likelihood Explanation

The attacker needs only one connected P2P session and the ability to generate arbitrary `PeerId` values (trivial — `PeerId::random()`). No PoW, no privileged role, no key material is required. The `ConnectionRequest` message is a standard P2P protocol message accepted from any peer. The attack is fully automatable and local-testable.

---

### Recommendation

1. **Cap `pending_delivered` at a fixed maximum size** (e.g., 1,024 entries). Reject new insertions when the cap is reached, or evict the oldest entry (LRU).
2. **Cap `forward_rate_limiter` key space** by calling `retain_recent()` periodically (e.g., in `notify()`) rather than only on disconnect.
3. **Add a global rate limit on `respond_delivered` insertions** independent of `from` peer ID, so the total insertion rate is bounded regardless of key diversity.

---

### Proof of Concept

```rust
// Pseudocode: attacker loop on one connected session
for i in 0..9000 {
    let fake_from = PeerId::random();
    send_connection_request(
        from = fake_from,
        to   = victim_local_peer_id,
        listen_addrs = [24 valid TCP/IP multiaddrs],
    );
    sleep(1.0 / 30.0); // respect outer rate limiter
}
// After 5 minutes: pending_delivered.len() == 9000 (no cleanup yet)
// assert!(protocol.pending_delivered.len() <= MAX_BOUND); // FAILS — no bound exists
```

Each iteration passes all guards: `HOLE_PUNCHING_INTERVAL` (new key), `forward_rate_limiter` (new bucket), and `ADDRS_COUNT_LIMIT` (≤24 addrs). The only throttle is the 30/sec outer limiter, which the loop respects. After one 5-minute window, `pending_delivered` holds 9,000 entries consuming ~13 MB per session, with no hard cap preventing further growth.

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L44-44)
```rust
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
```

**File:** network/src/protocols/hole_punching/mod.rs (L66-69)
```rust
    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.rate_limiter.retain_recent();
        self.forward_rate_limiter.retain_recent();
        debug!("HolePunching.disconnected session={}", context.session.id);
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

**File:** network/src/protocols/hole_punching/mod.rs (L172-174)
```rust
        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
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
