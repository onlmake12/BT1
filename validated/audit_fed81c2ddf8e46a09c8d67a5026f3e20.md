Audit Report

## Title
Unbounded `pending_delivered` HashMap Growth via Spoofed `from` PeerIDs in `ConnectionRequest` — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary
`HolePunching::pending_delivered` is an uncapped `HashMap<PeerId, (Vec<Multiaddr>, u64)>` pruned only in `notify()` every 5 minutes. An attacker with a standard P2P connection can insert one entry per message by supplying a distinct spoofed `from` PeerId in each `ConnectionRequest` targeting the victim node, bypassing the per-`from_peer_id` dedup guard entirely. At 30 messages/second per session across multiple sessions, this accumulates millions of entries before the first cleanup tick, causing OOM on the victim node.

## Finding Description
**No validation that `content.from` matches the actual session peer:**
`RequestContent::try_from()` parses `from` directly from message bytes with no check against the session's real peer ID. [1](#0-0) 

**Insertion path — `respond_delivered()` is called when `self_peer_id == content.to`:**
The victim node's own peer ID is public; the attacker trivially sets `content.to` to the victim's peer ID. [2](#0-1) 

**Dedup guard — only fires if the same `from_peer_id` is already present:**
A fresh `from_peer_id` per message means `pending_delivered.get(&from_peer_id)` always returns `None`, so the guard never triggers and insertion always proceeds. [3](#0-2) 

**Unconditional insertion:** [4](#0-3) 

**`forward_rate_limiter` — keyed by `(from, to, msg_item_id)`; each novel `from` is a new bucket, never throttled:**
The first check on any new key always passes. With a unique `from` per message, this limiter is entirely bypassed and also grows unboundedly (one entry per unique `(from, to)` pair), compounding the memory issue. [5](#0-4) 

**Outer `rate_limiter` — 30 msg/sec per session, the only effective throttle:**
Keyed by `(session_id, msg.item_id())`, this is the sole real constraint. [6](#0-5) 

**Cleanup — `pending_delivered.retain()` only runs in `notify()` at `CHECK_INTERVAL = 5 minutes`:** [7](#0-6) 

**`forward_rate_limiter` internal `HashMapStateStore` — `retain_recent()` only called in `disconnected()`, never in `notify()`:**
For a persistent attacker connection that never disconnects, the `HashMapStateStore<(PeerId, PeerId, u32)>` also accumulates one entry per unique `(from, to)` pair for the entire session lifetime. [8](#0-7) 

**No size cap on either map:** [9](#0-8) 

## Impact Explanation
Each `pending_delivered` entry holds a `PeerId` (~39 bytes) plus a `Vec<Multiaddr>` of at least 1 address (~50 bytes each), with HashMap overhead, totaling ~1.2–1.5 KB per entry. [10](#0-9) 

At 30 msg/sec × 300 sec = 9,000 entries per session. With `max_connections` ≈ 125 sessions: ~1.1 million entries × ~1.25 KB ≈ **~1.4 GB** of heap growth before the first `notify()` cleanup. The `forward_rate_limiter`'s `HashMapStateStore` adds further unbounded growth on top of this. This causes OOM on typical validator nodes.

**Matched impact:** *High — Vulnerabilities which could easily crash a CKB node* (10001–15000 points).

## Likelihood Explanation
- The attacker only needs a standard P2P connection; no consensus participation, no leaked keys, no PoW required.
- Generating arbitrary `PeerId` values (Ed25519 keypairs) is computationally trivial.
- The attack is sustainable: the attacker maintains connections and streams messages at 30/sec indefinitely.
- The `HolePunching` protocol is enabled by default on nodes that support NAT traversal.
- Establishing multiple connections up to `max_connections` is feasible for a motivated attacker using multiple IPs or Sybil peers.
- The victim's peer ID is public information, making the `content.to == self_peer_id` condition trivially satisfiable.

## Recommendation
1. **Validate `from` against session peer ID**: Reject `ConnectionRequest` messages where `content.from` does not match the actual peer ID of the sending session.
2. **Cap map size**: Enforce a hard upper bound (e.g., 1,024 entries) on `pending_delivered` and `inflight_requests`, rejecting new inserts when the cap is reached.
3. **Periodic rate-limiter cleanup**: Call `self.forward_rate_limiter.retain_recent()` inside `notify()`, not only in `disconnected()`.
4. **Per-session insert quota**: Track how many `pending_delivered` entries originated from each session and evict or reject when a per-session limit is exceeded.

## Proof of Concept
```
1. Attacker establishes up to max_connections P2P connections to victim node V (peer_id = V).
2. For each session, attacker generates N distinct Ed25519 keypairs → N distinct from_peer_ids F_1..F_N.
3. For each F_i, attacker sends:
     ConnectionRequest { from: F_i, to: V, listen_addrs: [<valid TCP IPv4 addr>], route: [], max_hops: 6 }
   at 30 msg/sec (within rate_limiter quota per session).
4. For each F_i:
   - forward_rate_limiter: new key (F_i, V, item_id) → allowed (novel bucket, first check always passes)
   - pending_delivered.get(F_i): absent → dedup guard skipped
   - remote_listens non-empty (valid TCP addr provided) → insertion proceeds
   - pending_delivered.insert(F_i, ([addr], now))
5. After 300 seconds per session: pending_delivered.len() == 9,000 (per session).
6. notify() has not fired yet (CHECK_INTERVAL = 5 min).
7. Across 125 sessions: ~1.1M entries, ~1.4 GB heap → OOM.

Invariant test: assert pending_delivered.len() <= BOUND after inserting at max rate for 5 min.
Currently fails: no such BOUND exists in the code.
```

### Citations

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

**File:** network/src/protocols/hole_punching/mod.rs (L27-47)
```rust
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes

type PendingDeliveredInfo = (Vec<Multiaddr>, u64);
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
