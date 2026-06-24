Audit Report

## Title
Unbounded `pending_delivered` Growth via Attacker-Controlled `from` PeerIds — (`network/src/protocols/hole_punching/component/connection_request.rs`, `mod.rs`)

## Summary
An attacker with a single valid P2P connection can send `ConnectionRequest` messages bearing distinct, attacker-generated `from` PeerIds targeting the victim node (`to = victim_peer_id`). Because `pending_delivered` is keyed by `from` and has no size cap, each distinct `from` inserts a new entry. The only cleanup runs every 5 minutes in `notify()`. With the outer rate limiter allowing 30 msgs/sec per session, a single session can insert up to 9,000 entries per cleanup window; with many inbound sessions the aggregate can exhaust heap memory and crash the node.

## Finding Description

**Data structure** (`mod.rs` L44): `pending_delivered: HashMap<PeerId, PendingDeliveredInfo>` is an unbounded map with no capacity limit. [1](#0-0) 

**Attacker-controlled `from`** (`connection_request.rs` L36–38): `from` is parsed directly from the message payload — it is fully attacker-controlled bytes. [2](#0-1) 

**Forward rate limiter bypass** (`connection_request.rs` L132–143): `forward_rate_limiter` is checked with key `(content.from, content.to, msg_item_id)`. Since `from` is attacker-supplied, each distinct `from` is a fresh key that passes the 1/sec quota. The outer `rate_limiter` (keyed by `(session_id, item_id)`) is the only real cap at 30/sec per session. [3](#0-2) 

**Unbounded insertion** (`connection_request.rs` L161–167, L234–237): The deduplication guard `pending_delivered.get(&from_peer_id)` only blocks re-insertion for the *same* `from`. With distinct `from` values, every message inserts a new entry unconditionally. [4](#0-3) [5](#0-4) 

**Cleanup only in `notify()`** (`mod.rs` L25, L172–175): `CHECK_INTERVAL = 5 minutes`. Entries younger than `TIMEOUT = 5 minutes` are retained, so all entries inserted during a window survive until the next tick. [6](#0-5) [7](#0-6) 

**`forward_rate_limiter` state also grows unboundedly**: `retain_recent()` on it is only called in `disconnected()` (L67–68), never periodically, so while the attacker holds the connection open the limiter's `HashMapStateStore` accumulates one entry per unique `(from, to, item_id)` key. [8](#0-7) 

**No banning triggered**: `TooManyRequests` and `Ignore` return codes do not call `should_ban()`, so the attacker is never disconnected or banned. [9](#0-8) 

## Impact Explanation

Per session: 30 msgs/sec × 300 sec = **9,000 entries** per 5-minute window. Each entry stores a `PeerId` key (~39 bytes) plus a `Vec<Multiaddr>` of up to 24 TCP addresses (~50 bytes each ≈ 1.2 KB) plus a `u64` timestamp — roughly **~11 MB per session per window**. CKB nodes accept up to ~125 inbound peers by default, yielding an aggregate of **~1.4 GB** before the first cleanup tick. This can exhaust heap memory and crash the node.

This matches the allowed impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

- Requires only a single valid P2P connection; no privilege, no PoW, no key material.
- `from` PeerIds are arbitrary bytes generated locally by the attacker.
- The attacker must supply at least one valid TCP IPv4/IPv6 listen address (trivially satisfied with their own IP).
- `send_message_to` back to the attacker's session succeeds since the attacker is connected.
- No banning is triggered by `TooManyRequests` or `Ignore` status codes.
- The attack is repeatable across multiple sessions and across cleanup windows.

## Recommendation

1. **Cap `pending_delivered` size** with a hard `HashMap::len()` guard before insertion (e.g., reject if `len() >= MAX_PENDING_DELIVERED`), or use an LRU eviction policy.
2. **Key `forward_rate_limiter` by `(session_id, to, item_id)` instead of `(from, to, item_id)`** — the session is the only attacker-uncontrollable identity, making the forward limiter equivalent to the outer limiter and eliminating the bypass.
3. **Periodically call `forward_rate_limiter.retain_recent()`** inside `notify()` alongside the existing `pending_delivered` cleanup to prevent unbounded limiter state growth.
4. **Reduce `CHECK_INTERVAL`** or add an intermediate size-based eviction trigger.

## Proof of Concept

```rust
// Pseudocode unit test
let mut protocol = HolePunching::new(network_state);
let session_id = PeerIndex::new(1);
let victim_peer_id = protocol.network_state.local_peer_id().clone();

for i in 0..9000 {
    let from = PeerId::random(); // distinct each iteration — new forward_rate_limiter key
    let msg = build_connection_request(
        from,
        victim_peer_id.clone(),
        vec![valid_tcp_ipv4_addr()], // passes remote_listens filter
    );
    // outer rate_limiter: (session_id, item_id=0) — 30/sec, passes within burst
    // forward_rate_limiter: (from, to, 0) — new key each time, passes 1/sec quota
    // respond_delivered: pending_delivered.get(&from) → None → inserts unconditionally
    protocol.received(ctx_with_session(session_id), msg).await;
}

// After 5-minute window: all 9,000 entries still present (TIMEOUT not yet elapsed)
assert_eq!(protocol.pending_delivered.len(), 9000);
// With 125 sessions: ~1.4 GB heap consumed before first notify() cleanup
```

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L25-28)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L44-46)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L145-166)
```rust
        if let Some(ban_time) = status.should_ban() {
            error!(
                "process {} from {}; ban {:?} since result is {}",
                item_name, session_id, ban_time, status
            );
            self.network_state.ban_session(
                &context.control().clone().into(),
                session_id,
                ban_time,
                status.to_string(),
            );
        } else if status.should_warn() {
            warn!(
                "process {} from {}; result is {}",
                item_name, session_id, status
            );
        } else if !status.is_ok() {
            debug!(
                "process {} from {}; result is {}",
                item_name, session_id, status
            );
        }
```

**File:** network/src/protocols/hole_punching/mod.rs (L172-175)
```rust
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
