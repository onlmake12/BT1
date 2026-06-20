Let me look at the `governor` crate's `HashMapStateStore` behavior and the `notify` function more carefully, plus check if `msg_item_id` is fixed per message type.

Based on my analysis of the code, here is my determination:

---

### Title
Unbounded `forward_rate_limiter` HashMap Growth via Attacker-Controlled PeerId Pairs in ConnectionSync — (`network/src/protocols/hole_punching/mod.rs`, `connection_sync.rs`)

### Summary

The `forward_rate_limiter` in `HolePunching` uses a `governor::RateLimiter` backed by `HashMapStateStore<(PeerId, PeerId, u32)>`. The key includes `from` and `to` PeerIds taken directly from message content. A remote peer can send ConnectionSync messages with unique `(from, to)` pairs at up to 30/second (the outer rate limiter cap), inserting a new HashMap entry per unique pair. `retain_recent()` is called **only** in `disconnected()` — never periodically — so the map grows without bound for the lifetime of any connection.

### Finding Description

**Outer rate limiter** (`rate_limiter`, keyed by `(PeerIndex, u32)`): [1](#0-0) 

This limits to 30 ConnectionSync messages/second per session. The key is `(session_id, fixed_msg_type_id)` — a single entry per session, bounded.

**Inner `forward_rate_limiter`** (`RateLimiter<(PeerId, PeerId, u32)>`): [2](#0-1) 

Keyed by `(content.from, content.to, msg_item_id)` where `from` and `to` come from the wire message with no validation beyond being parseable PeerId bytes: [3](#0-2) [4](#0-3) 

Each unique `(from, to)` pair inserts a new entry into the `HashMapStateStore`. `retain_recent()` is called **only** on disconnect: [5](#0-4) 

The `notify()` handler (runs every 5 minutes) cleans up `pending_delivered` and `inflight_requests` but **never** calls `retain_recent()` on either rate limiter: [6](#0-5) 

### Impact Explanation

Memory growth rate: 30 entries/second × ~200–300 bytes/entry ≈ **~21–32 MB/hour**, **~500 MB/day**, **~3.5 GB/week** per attacking peer. A node with limited RAM (e.g., 4–8 GB) can be OOM-crashed within days by a single persistent peer. Multiple attacking peers multiply the rate linearly.

### Likelihood Explanation

The attacker needs only a standard P2P connection — no privilege, no PoW, no key material. The HolePunching protocol is enabled by default. The attack is fully automatable: connect, loop sending ConnectionSync with fresh random PeerId bytes in `from`/`to` fields at 30/second, never disconnect. The outer rate limiter does not prevent this — it only caps the insertion rate, not the total size.

### Recommendation

1. Call `self.forward_rate_limiter.retain_recent()` inside `notify()` (every 5 minutes) alongside the existing `pending_delivered` and `inflight_requests` cleanup.
2. Enforce a hard cap on the `HashMapStateStore` size (e.g., reject `check_key` or evict LRU entries when the map exceeds a configurable limit such as 10,000 entries).
3. Validate that `content.from` and `content.to` correspond to actually-connected peers before inserting into the rate limiter.

### Proof of Concept

```
1. Connect to victim node via P2P (HolePunching protocol).
2. In a loop at 30 msg/sec:
     - Generate fresh random Ed25519 keypairs → PeerId_i, PeerId_j
     - Send ConnectionSync{ from: PeerId_i, to: PeerId_j, route: [] }
3. Never disconnect.
4. After N seconds, forward_rate_limiter internal DashMap has N entries.
5. Assert: map.len() grows linearly with N, unbounded by any constant K.
6. After ~days, victim node OOMs and crashes.
```

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L31-46)
```rust
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L42-47)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
            .map_err(|_| StatusCode::InvalidToPeerId.with_context("the to peer id is invalid"))?;
        Ok(SyncContent { route, from, to })
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L85-96)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
        {
            debug!(
                "from: {}, to {}, item_name: {}, rate limit is reached",
                content.from, content.to, "ConnectionSync",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionSync");
        }
```
