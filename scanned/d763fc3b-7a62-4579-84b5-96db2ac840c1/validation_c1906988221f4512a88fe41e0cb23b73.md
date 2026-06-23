Now I have all the code needed to make a determination. Let me trace the full attack path precisely.

**Rate limiter keying** — `received()` checks `(session_id, msg.item_id())`: [1](#0-0) 

This limits to 30 `ConnectionRequest` messages/sec **per session**, not per `from` PeerId.

**`forward_rate_limiter` keying** — `execute()` checks `(content.from, content.to, msg_item_id)`: [2](#0-1) 

With unique `from` PeerIds per message, each message gets its own fresh bucket — this limiter is fully bypassed.

**Deduplication in `respond_delivered()`** — only skips if the **same** `from_peer_id` was seen recently: [3](#0-2) 

With unique `from` PeerIds, this check never fires.

**Unbounded insert** — after a successful send-back, every unique `from_peer_id` is inserted with no size cap: [4](#0-3) 

**Cleanup only in `notify()`** — runs every `CHECK_INTERVAL = 300s`, retaining entries younger than `TIMEOUT = 300s`: [5](#0-4) 

The `pending_delivered` map is unbounded between `notify()` invocations.

---

### Title
Unbounded `pending_delivered` HashMap Growth via Unique-`from` `ConnectionRequest` Flood — (`network/src/protocols/hole_punching/mod.rs`)

### Summary
An unprivileged remote peer can exhaust heap memory on a victim node by sending `ConnectionRequest` messages at 30 msg/sec with unique `from` PeerIds targeting the victim's own `local_peer_id`. Each message bypasses all rate-limiting and deduplication guards and inserts a new entry into `pending_delivered`, which has no size cap and is only evicted every 5 minutes.

### Finding Description
`HolePunching::pending_delivered` is a `HashMap<PeerId, (Vec<Multiaddr>, u64)>` with no capacity bound.

The insertion path is:

```
received()
  → rate_limiter.check_key((session_id, item_id))   // 30/sec per session — passes
  → ConnectionRequestProcess::execute()
      → forward_rate_limiter.check_key((from, to, item_id))  // 1/sec per (from,to) — bypassed with unique `from`
      → self_peer_id == content.to  // victim is the target
      → respond_delivered(from_peer_id, ...)
          → pending_delivered.get(&from_peer_id)  // miss — new unique key
          → send_message_to(peer, ...)             // succeeds (attacker is connected)
          → pending_delivered.insert(from_peer_id, (remote_listens, now))  // unbounded
```

The only eviction is `pending_delivered.retain(...)` inside `notify()`, which fires every `CHECK_INTERVAL = 300 s`. Between two `notify()` calls, an attacker can insert `30 × 300 = 9 000` entries per session. Each entry stores up to `ADDRS_COUNT_LIMIT = 24` `Multiaddr` objects. With multiple concurrent sessions the rate multiplies linearly.

The sole prerequisite is that the attacker knows (or guesses) the victim's `PeerId`, which is publicly advertised on the P2P network.

### Impact Explanation
- **Heap exhaustion / OOM**: 9 000 entries × 24 `Multiaddr` values × ~100 bytes each ≈ ~21 MB per 5-minute window per session. Multiple sessions compound this.
- **HashMap slowdown**: Large `HashMap` degrades all operations that touch `pending_delivered` (insert, lookup in `respond_delivered`, `retain` in `notify()`).
- **Network congestion**: The victim also sends a `ConnectionRequestDelivered` reply for every accepted message, amplifying outbound traffic.

### Likelihood Explanation
The attack requires only a single standard P2P connection to the victim. The victim's `PeerId` is publicly discoverable. No special privileges, keys, or hashpower are needed. The attacker constructs valid `ConnectionRequest` messages with random `from` PeerIds and at least one valid TCP `listen_addr` — both trivially achievable.

### Recommendation
1. **Cap `pending_delivered`**: Enforce a maximum size (e.g., 1 000 entries). On overflow, reject new insertions or evict the oldest entry.
2. **Rate-limit insertions per session**: Track how many `pending_delivered` entries originated from each session and cap that count.
3. **Shorten or decouple the eviction interval**: Run `pending_delivered` eviction more frequently than `CHECK_INTERVAL`, or evict eagerly on insertion when the map exceeds a threshold.
4. **Validate `from` PeerId against the sending session**: Reject `ConnectionRequest` messages where `from` does not match the session's authenticated peer identity, eliminating spoofed `from` PeerIds entirely.

### Proof of Concept
```
1. Connect to victim node V (obtain its PeerId P from the P2P discovery layer).
2. From a single session, send 30 ConnectionRequest messages per second:
     from = PeerId::random()   // unique each time
     to   = P                  // victim's local_peer_id
     listen_addrs = [/ip4/1.2.3.4/tcp/1234]  // one valid TCP addr
     max_hops = 6
3. After 300 seconds (before the next notify() fires):
     assert pending_delivered.len() == 9000
     assert no eviction has occurred
4. Observe victim heap growth of ~21 MB and degraded HashMap performance.
```

### Citations

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
