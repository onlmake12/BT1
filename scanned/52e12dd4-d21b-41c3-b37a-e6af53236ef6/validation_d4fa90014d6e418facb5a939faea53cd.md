The code confirms all the key claims. Let me verify the rate limiter implementation details.

Audit Report

## Title
Unbounded `forward_rate_limiter` and `pending_delivered` Heap Growth via Unique Attacker-Controlled `from` Peer IDs in `ConnectionRequestProcess::execute` — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary

The `HolePunching` protocol's two secondary deduplication guards — `forward_rate_limiter` and `pending_delivered` — are both keyed on the attacker-controlled `from` field of `ConnectionRequest` messages. An unprivileged remote peer can send a stream of messages each with a unique random `from` peer ID, causing both structures to grow without effective bound. The `forward_rate_limiter`'s `HashMapStateStore` is never cleaned during an active session (only on `disconnected()`), making it a truly unbounded growth vector. This can exhaust heap memory and crash the victim CKB node.

## Finding Description

**Outer `rate_limiter`** is keyed by `(session_id, msg.item_id())` and correctly caps one session to 30 `ConnectionRequest` messages per second. This guard is not bypassed. [1](#0-0) 

**`forward_rate_limiter`** is typed as `RateLimiter<(PeerId, PeerId, u32)>` and checked with the key `(content.from, content.to, self.msg_item_id)`. [2](#0-1) [3](#0-2) 

Because `content.from` is fully attacker-controlled, each message with a fresh random `from` creates a brand-new bucket in the `HashMapStateStore`. The 1 req/sec quota is never reached for any individual key, so the check always passes.

**`pending_delivered`** is a `HashMap<PeerId, PendingDeliveredInfo>` checked and inserted using `from_peer_id` as the key. [4](#0-3) [5](#0-4) 

Since each message carries a unique `from`, `pending_delivered.get(&from_peer_id)` always returns `None`, bypassing the 2-minute cooldown entirely. Insertion follows unconditionally after a successful send: [6](#0-5) 

**`pending_delivered` cleanup** occurs only in `notify()` every 5 minutes (`CHECK_INTERVAL`), retaining entries younger than `TIMEOUT` (5 min). This means up to 30 × 300 = 9,000 entries accumulate per session per window before any eviction. [7](#0-6) [8](#0-7) 

**`forward_rate_limiter` is never cleaned during an active session.** `retain_recent()` is called only in `disconnected()`: [9](#0-8) 

For a long-lived attacker connection, the `HashMapStateStore` grows at 30 entries/sec indefinitely. Each entry holds a `(PeerId, PeerId, u32)` key (~82 bytes) plus HashMap overhead and governor state (~60–80 bytes), totalling ~150 bytes per entry.

**The `respond_delivered` path is triggered** when `self_peer_id == &content.to`, which the attacker satisfies by setting `to` to the victim's publicly known peer ID. [10](#0-9) 

## Impact Explanation

**`forward_rate_limiter` (unbounded for long-lived connections):**
- 30 entries/sec × 3,600 sec = ~108,000 entries/hour per session ≈ ~16 MB/hr per session
- Over 24 hours per session: ~240 MB
- With multiple inbound sessions (typical `max_inbound` ~125): 125 × 16 MB/hr = **~2 GB/hr**

**`pending_delivered` (bounded per 5-min window but still significant):**
- Up to 9,000 entries per session per window; each entry holds a `PeerId` key + `Vec<Multiaddr>` + `u64`
- With 125 sessions: ~1,125,000 entries ≈ ~225 MB per window

The combined heap growth from `forward_rate_limiter` alone is sufficient to exhaust available memory on a production node over hours, causing an OOM crash. This matches the allowed CKB bounty impact: **High — Vulnerabilities which could easily crash a CKB node** (10,001–15,000 points).

## Likelihood Explanation

The attack requires only:
1. A single TCP connection to the victim (no authentication, no PoW, no key material).
2. `ConnectionRequest` messages with `to=victim_peer_id` (publicly known from the P2P network), a fresh random valid multihash `from` per message, and at least one valid TCP IPv4/IPv6 `listen_addr`.
3. Sustaining the connection for hours.

All three conditions are trivially achievable by any remote peer. The outer rate limiter (30/sec) is not bypassed — it simply sets the growth rate. The attack is repeatable and requires no victim interaction or mistakes.

## Recommendation

1. **Key `forward_rate_limiter` on `(session_id, to, item_id)`** instead of `(from, to, item_id)`. The `from` field is attacker-controlled and unbounded; `session_id` is bounded by the number of active connections.
2. **Call `self.forward_rate_limiter.retain_recent()` inside `notify()`**, not only in `disconnected()`, to bound growth during long-lived sessions.
3. **Bound `pending_delivered` by size** (e.g., a `MAX_PENDING_DELIVERED` constant of 1,024) and reject insertions when the map is full, or use an LRU eviction policy.
4. **Add a per-session cap on `pending_delivered` insertions**, since a single session can fill the map at the full 30/sec rate.

## Proof of Concept

```rust
// Minimal unit test sketch
let mut protocol = HolePunching::new(network_state.clone());
let session_id = PeerIndex::new(1);
let victim_peer_id = network_state.local_peer_id().clone();

for _ in 0..9000 {
    let from = PeerId::random(); // unique per iteration — bypasses both guards
    let msg = build_connection_request(
        from,
        victim_peer_id.clone(),
        vec!["/ip4/1.2.3.4/tcp/8115".parse().unwrap()],
    );
    // outer rate_limiter: 30/sec per (session_id, item_id) — real cap, sets growth rate
    // forward_rate_limiter: (from, to, item_id) — new key each time, always passes
    // pending_delivered.get(&from) == None — always passes, inserts unconditionally
    ConnectionRequestProcess::new(msg, &mut protocol, session_id, &control, 0)
        .execute()
        .await;
}

// pending_delivered holds up to 9,000 entries (bounded by 5-min window)
assert!(protocol.pending_delivered.len() <= 9000);
// forward_rate_limiter internal HashMapStateStore also holds 9,000 entries,
// never cleaned until disconnect — grows unboundedly for long-lived sessions
```

The `forward_rate_limiter` state is not directly inspectable without governor internals, but its growth can be confirmed by measuring process RSS before and after the loop, or by instrumenting `HashMapStateStore` entry count via governor's `len()` method if exposed.

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L25-28)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L44-44)
```rust
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-152)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
        } else if content.max_hops == 0u8 {
            StatusCode::ReachedMaxHops.into()
        } else {
            self.forward_message(self_peer_id, &content.to).await
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
