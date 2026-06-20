### Title
Unbounded `pending_delivered` and `forward_rate_limiter` Growth via Spoofed `from` Peer IDs in `ConnectionRequestProcess::execute` — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

An unprivileged remote peer can send a stream of `ConnectionRequest` messages with `to=victim_peer_id` and a distinct random `from` `PeerId` per message. Because the `forward_rate_limiter` is keyed by `(from, to, msg_item_id)` and the `pending_delivered` dedup check is also keyed by `from_peer_id`, every unique `from` value bypasses both guards and inserts a new entry into `HolePunching::pending_delivered`. The only eviction is a time-based sweep in `notify()` every 5 minutes. Additionally, `forward_rate_limiter` itself is never cleaned up in `notify()` — only in `disconnected()` — so a persistent attacker causes it to grow without bound.

---

### Finding Description

**Rate limiter keying mismatch**

The outer `rate_limiter` is keyed by `(session_id, item_id)` and caps at 30 msgs/sec per session: [1](#0-0) 

The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)` at 1 msg/sec: [2](#0-1) [3](#0-2) 

Since `from` is attacker-controlled and not authenticated, each unique `from` value creates a fresh bucket in the `HashMapStateStore`, so the 1/sec cap is trivially bypassed by rotating `from` values.

**`pending_delivered` dedup check is keyed by `from`**

In `respond_delivered`, the only dedup guard is: [4](#0-3) 

A new `from` peer ID is never in the map, so the guard is always skipped. After a successful `send_message_to` back to the attacker's session, the entry is unconditionally inserted: [5](#0-4) 

**No size cap on `pending_delivered`; cleanup only in `notify()` every 5 minutes** [6](#0-5) [7](#0-6) 

Within a single 5-minute window, one session can insert up to 30 × 300 = **9,000 entries**. With N concurrent inbound sessions (up to the node's inbound connection limit), the map grows to 30 × N × 300 entries before the first eviction.

**`forward_rate_limiter` grows without bound on persistent connections**

`retain_recent()` is only called in `disconnected()`: [8](#0-7) 

`notify()` does not call `retain_recent()` on either rate limiter. A persistent attacker who never disconnects causes `forward_rate_limiter`'s `HashMapStateStore` to accumulate entries at 30/sec indefinitely — 2.6 M entries after 24 hours, each holding a `(PeerId, PeerId, u32)` key plus governor cell state.

---

### Impact Explanation

- **`pending_delivered`**: bounded per 5-minute window but has no absolute size cap. With ~125 inbound sessions (typical CKB inbound limit), the map can hold ~1.1 M entries × ~300 bytes ≈ **~330 MB** before the first cleanup tick.
- **`forward_rate_limiter`**: truly unbounded for persistent connections. After 24 hours of a single-session attack: ~2.6 M entries × ~100 bytes ≈ **~260 MB**; after one week ≈ **~1.8 GB**.
- Combined memory pressure can exhaust heap, crash the victim node, fragment the P2P network, and cause consensus deviation by isolating the node from the network.

---

### Likelihood Explanation

The attack requires only a single inbound TCP connection and the ability to craft `ConnectionRequest` messages with arbitrary `from` bytes — no authentication, no PoW, no privileged role. The `from` field is parsed as a raw `PeerId` multihash with no registry check against actually-connected peers: [9](#0-8) 

The attacker does not need to be the real peer identified by `from`. The attack is fully local-testable.

---

### Recommendation

1. **Cap `pending_delivered` size**: enforce a maximum entry count (e.g., 1,024) and reject new insertions when the cap is reached, or use an LRU eviction policy.
2. **Call `retain_recent()` in `notify()`**: add `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside `notify()` so the rate limiter state is periodically pruned regardless of disconnects.
3. **Validate `from` against connected peers**: before entering `respond_delivered`, verify that `content.from` corresponds to a peer actually known to the network (e.g., present in the peer registry), making spoofed `from` values immediately rejectable.
4. **Rate-limit insertions into `pending_delivered` by session**, not just by `(from, to)`, to prevent a single session from flooding the map.

---

### Proof of Concept

```rust
// Pseudocode unit test
let mut protocol = HolePunching::new(network_state.clone());
let victim_peer_id = network_state.local_peer_id().clone();
let session_id = SessionId::new(1);

for i in 0..9000 {
    let from = PeerId::random(); // unique each iteration
    let msg = build_connection_request(from, victim_peer_id.clone(), vec!["/ip4/1.2.3.4/tcp/1234".parse().unwrap()]);
    // outer rate_limiter: 30/sec per (session_id, item_id) — throttle to 30/sec
    // forward_rate_limiter: unique (from, to, item_id) each time → always passes
    // pending_delivered.get(&from) → None → dedup skipped
    ConnectionRequestProcess::new(msg, &mut protocol, session_id, &control, ITEM_ID)
        .execute()
        .await;
}

assert_eq!(protocol.pending_delivered.len(), 9000); // grows proportionally
// forward_rate_limiter internal map also has 9000 entries, never cleaned until disconnect
```

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L25-28)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
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

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
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
