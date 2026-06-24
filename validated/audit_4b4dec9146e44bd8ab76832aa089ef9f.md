Audit Report

## Title
Unbounded `pending_delivered` HashMap Growth via Spoofed `from` PeerIDs in `ConnectionRequest` — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary
An unprivileged remote peer can cause the `pending_delivered` HashMap in `HolePunching` to grow without bound between `notify()` cleanup ticks by sending `ConnectionRequest` messages addressed to the victim node (`to = victim_peer_id`) with a distinct attacker-generated `from_peer_id` per message. The only per-session rate limit (30 msg/sec) throttles throughput but does not cap map size, and the only dedup guard is keyed on `from_peer_id` — which the attacker rotates freely. This enables memory exhaustion and node crash.

## Finding Description
`HolePunching::pending_delivered` is a `HashMap<PeerId, (Vec<Multiaddr>, u64)>` declared with no size bound. [1](#0-0) 

Entries are inserted in `respond_delivered()` when the victim is the `to` target of a `ConnectionRequest`: [2](#0-1) 

The only guard against repeated insertion for the same key is a 2-minute dedup check keyed on `from_peer_id`: [3](#0-2) 

An attacker who rotates `from_peer_id` per message bypasses this entirely — each new `from` value is absent from the map, so the check never fires.

The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)` at 1/sec: [4](#0-3) 

Because each distinct `from_peer_id` is a new bucket, this limiter never fires for novel `from` values and does not bound map growth.

The outer `rate_limiter` is keyed by `(session_id, msg.item_id())` at 30/sec: [5](#0-4) 

This limits throughput per session but does not cap the number of distinct `from_peer_id` keys inserted into `pending_delivered`.

The only cleanup is `retain()` inside `notify()`, which fires every `CHECK_INTERVAL = 5 minutes`: [6](#0-5) [7](#0-6) 

Additionally, `forward_rate_limiter.retain_recent()` is only called in `disconnected()`, never in `notify()`: [8](#0-7) 

So the `HashMapStateStore<(PeerId, PeerId, u32)>` inside `forward_rate_limiter` also accumulates one entry per unique `(from, to)` pair for the entire lifetime of a persistent connection.

The `remote_listens` filtering at L196–215 requires valid TCP/IPv4 or IPv6 addresses, which is trivially satisfiable by the attacker. The `listen_addrs` length check at L115–118 requires 1–24 addresses, also trivially satisfiable. [9](#0-8) 

## Impact Explanation
Each `pending_delivered` entry holds a `PeerId` (~39 bytes) plus a `Vec<Multiaddr>` of up to `ADDRS_COUNT_LIMIT = 24` addresses (~50–100 bytes each), totaling ~1.2–2.4 KB per entry plus allocator overhead. [10](#0-9) 

At 30 msg/sec × 300 sec = **9,000 entries per session** before the first `notify()` cleanup. Across `max_connections` peers (typically 125+), the total reaches ~1.1 million entries and ~2.8 GB of heap growth before any cleanup fires. This causes OOM on typical validator nodes.

This matches the **High** impact category: *"Vulnerabilities which could easily crash a CKB node."*

## Likelihood Explanation
- The attacker only needs a standard P2P connection to a node with `HolePunching` enabled — no consensus participation, no leaked keys, no Sybil majority required.
- Generating arbitrary `PeerId` values (Ed25519 keypairs) is computationally trivial.
- The attack is sustainable: the attacker maintains the connection and streams messages at 30/sec indefinitely.
- Multiple attacker sessions (from different IPs or using multiple connections) multiply the effect linearly.
- No victim mistake or external context is required.

## Recommendation
1. **Cap map size**: Enforce a hard upper bound (e.g., 1,024 entries) on `pending_delivered` and `inflight_requests`, rejecting new inserts when the cap is reached.
2. **Per-session insert quota**: Track how many `pending_delivered` entries originated from each session and evict or reject when a per-session limit is exceeded.
3. **Periodic rate-limiter cleanup**: Call `self.forward_rate_limiter.retain_recent()` inside `notify()`, not only in `disconnected()`, to prevent unbounded growth of the `HashMapStateStore`.

## Proof of Concept
```
1. Attacker establishes one or more P2P connections to victim node V (peer_id = V).
2. Attacker generates N distinct Ed25519 keypairs → N distinct from_peer_ids F_1..F_N.
3. For each F_i, attacker sends at 30 msg/sec (within rate_limiter quota):
     ConnectionRequest { from: F_i, to: V, listen_addrs: [<valid TCP IPv4 addr>], route: [], max_hops: 6 }
4. For each F_i:
   - forward_rate_limiter: new key (F_i, V, item_id) → allowed (1/sec quota, fresh bucket)
   - pending_delivered.get(F_i): absent → dedup check skipped
   - remote_listens non-empty after TCP filter → insert proceeds
   - pending_delivered.insert(F_i, ([addr], now))
5. After 300 seconds (before notify() fires): pending_delivered.len() == 9,000 per session.
6. With 125 sessions: ~1.1M entries, ~2.8 GB heap → OOM / node crash.

Invariant test (currently fails — no such bound exists):
  assert!(pending_delivered.len() <= SOME_BOUND);
```

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L25-25)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
```

**File:** network/src/protocols/hole_punching/mod.rs (L27-30)
```rust
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes

type PendingDeliveredInfo = (Vec<Multiaddr>, u64);
```

**File:** network/src/protocols/hole_punching/mod.rs (L43-44)
```rust
    // Delivered timestamp recorded
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
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

**File:** network/src/protocols/hole_punching/mod.rs (L173-175)
```rust
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L115-118)
```rust
        if content.listen_addrs.len() > ADDRS_COUNT_LIMIT || content.listen_addrs.is_empty() {
            return StatusCode::InvalidListenAddrLen
                .with_context("the listen address count is too large or empty");
        }
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
