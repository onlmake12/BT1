### Title
Unbounded `pending_delivered` HashMap Growth via Spoofed `from` PeerIds in `ConnectionRequest` — (`network/src/protocols/hole_punching/component/connection_request.rs`)

### Summary

A connected unprivileged peer can flood the victim node (when it is the `to` target) with `ConnectionRequest` messages carrying unique, attacker-controlled `from` PeerIds. Each message bypasses the per-(from,to) `forward_rate_limiter` and the per-`from_peer_id` dedup guard in `respond_delivered()`, inserting a new entry into `pending_delivered: HashMap<PeerId, PendingDeliveredInfo>` with no capacity cap. The map is only pruned in `notify()` every 5 minutes, allowing up to 9,000 entries to accumulate from a single session before the next prune.

---

### Finding Description

**Rate limiters in play:**

1. **`rate_limiter`** — keyed by `(session_id, msg.item_id())`, capped at **30 req/sec per session**. [1](#0-0) 

2. **`forward_rate_limiter`** — keyed by `(content.from, content.to, msg_item_id)`, capped at **1 req/sec per (from, to) pair**. [2](#0-1) 

**Attack path through `execute()`:**

When `self_peer_id == content.to`, `execute()` calls `respond_delivered(content.from, ...)`. [3](#0-2) 

Inside `respond_delivered()`, the only dedup guard checks `pending_delivered.get(&from_peer_id)`: [4](#0-3) 

With a **unique `from` PeerId per message**, this guard always misses, and the function proceeds to insert unconditionally: [5](#0-4) 

The `forward_rate_limiter` is also bypassed because each unique `from` PeerId creates a new key `(from, to, item_id)`: [6](#0-5) 

**The only effective bound is the per-session `rate_limiter` at 30/sec.** Over the 5-minute `CHECK_INTERVAL`, a single session can insert `30 × 300 = 9,000` entries.

**Pruning only happens in `notify()`:** [7](#0-6) 

`pending_delivered` is initialized as an unbounded `HashMap::new()`: [8](#0-7) 

---

### Impact Explanation

Each `PendingDeliveredInfo` entry is `(Vec<Multiaddr>, u64)`. The attacker can supply up to `ADDRS_COUNT_LIMIT` (24) TCP addresses per message. [9](#0-8) 

- Per entry: ~38 bytes (PeerId key) + 24 × ~30 bytes (Multiaddr) + 8 bytes (timestamp) ≈ 766 bytes
- 9,000 entries ≈ **~6.9 MB per attacker session** before the next prune
- With multiple concurrent attacker sessions, this scales linearly

The heap growth is bounded per-session by the rate limiter, but there is **no absolute cap** on `pending_delivered.len()`. The invariant that protocol state maps must be bounded in size is violated.

---

### Likelihood Explanation

- Attacker only needs a standard P2P connection to the victim — no privilege required.
- The victim's peer ID is publicly discoverable via the P2P identify protocol.
- The attacker sets `content.to` to the victim's peer ID and `content.from` to a fresh random PeerId per message.
- The attacker must supply at least one valid TCP `listen_addr` with an IPv4/IPv6 component (trivially satisfied, e.g., `127.0.0.1:1234`).
- The `send_message_to` back to the attacker's session must succeed; the attacker keeps the session open.

---

### Recommendation

Add a capacity cap to `pending_delivered`. For example, reject the insert in `respond_delivered()` if `pending_delivered.len() >= MAX_PENDING_DELIVERED` (e.g., 256 or 1024). Alternatively, key the rate limiter on `(session_id, from_peer_id)` rather than only `(session_id, msg.item_id())` to prevent a single session from inserting unbounded unique keys.

---

### Proof of Concept

```rust
// State test sketch
let mut hp = HolePunching::new(network_state_configured_as_victim());
for _ in 0..10_000 {
    let from = PeerId::random();
    let msg = build_connection_request(
        from,
        victim_peer_id,
        vec!["/ip4/127.0.0.1/tcp/1234".parse().unwrap()],
    );
    hp.received(/* context */, msg).await;
}
assert!(hp.pending_delivered.len() > 9_000); // passes before notify() fires
```

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L27-30)
```rust
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes

type PendingDeliveredInfo = (Vec<Multiaddr>, u64);
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

**File:** network/src/protocols/hole_punching/mod.rs (L249-252)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (CHECK_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/hole_punching/mod.rs (L279-279)
```rust
            pending_delivered: HashMap::new(),
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
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
