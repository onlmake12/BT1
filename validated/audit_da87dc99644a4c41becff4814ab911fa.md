Audit Report

## Title
Unbounded `forward_rate_limiter` HashMap Growth via Spoofed `from=local_peer_id` in `ConnectionRequestDelivered` — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

## Summary
The `HolePunching` protocol's `forward_rate_limiter` (`HashMapStateStore<(PeerId, PeerId, u32)>`) inserts a new state cell for every previously-unseen `(from, to, item_id)` key. An attacker who spoofs `from = local_peer_id` and varies `to` per message can insert entries at 30/second per session with no eviction until disconnect, because `retain_recent()` is never called during a live connection. The terminal code path returns `StatusCode::Ignore` (501), which does not satisfy `should_ban()`, so the attacker is never disconnected.

## Finding Description

**Root cause — `forward_rate_limiter` grows without bound during a live connection.**

`forward_rate_limiter` is declared as `RateLimiter<(PeerId, PeerId, u32)>` backed by `governor::state::keyed::HashMapStateStore`, which lazily inserts a new state cell for every previously-unseen key on `check_key`. [1](#0-0) 

At `connection_request_delivered.rs:134–137`, `check_key` is called unconditionally before any routing logic, using the attacker-controlled `content.from` and `content.to` fields: [2](#0-1) 

With a unique `to` per message, every call inserts a new HashMap entry.

**Why the spoofed path reaches `StatusCode::Ignore` with no ban.**

With `route = []`, `content.route.last()` is `None`, entering the `None` branch at line 147. The check at line 151 (`if self_peer_id != &content.from`) is `false` when `from = local_peer_id`, so the forward path is skipped. `inflight_requests.remove(&content.to)` returns `None` for any unknown `to`, and line 175 returns `StatusCode::Ignore`. [3](#0-2) 

`StatusCode::Ignore = 501` falls in the 500–599 range. `should_ban()` only covers 400–499, so no ban is issued: [4](#0-3) 

**Why entries are never evicted during a live connection.**

`retain_recent()` is called only in `disconnected` (mod.rs:67–68). The `notify` handler (mod.rs:169–244), which fires every `CHECK_INTERVAL` (5 minutes), cleans up `pending_delivered` and `inflight_requests` but never calls `retain_recent()` on either rate limiter: [5](#0-4) [6](#0-5) 

**Outer rate limiter does not prevent map growth.**

The outer `rate_limiter` is keyed by `(session_id, msg.item_id())` and allows 30 messages/second per session. This throttles the insertion rate into `forward_rate_limiter` but does not prevent it — 30 new entries/second per session is the guaranteed growth rate. [7](#0-6) 

## Impact Explanation

Memory exhaustion leading to node crash. Each `forward_rate_limiter` entry holds two `PeerId` values (~39 bytes each), a `u32`, HashMap overhead, and `governor` internal state (~200–300 bytes total). At 30 entries/second per session, the map grows at ~6–9 KB/second. With multiple concurrent attacker sessions, growth scales linearly. Over a sustained connection (hours), this exhausts available memory and crashes the node.

**Severity: High — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

- Any peer that can establish a standard P2P connection can trigger this — no privilege, no key, no proof-of-work required.
- The local peer ID is publicly advertised via the identify protocol, making `from = local_peer_id` trivially obtainable.
- The attack requires only sustained message sending with unique `to` peer IDs (random bytes are sufficient).
- The outer rate limiter does not prevent the attack; it only sets the insertion rate at 30/second per session.
- Multiple sessions multiply the growth rate linearly.
- The attacker is never banned, so the connection can be maintained indefinitely.

## Recommendation

1. **Call `retain_recent()` periodically**: In the `notify` handler (`mod.rs:169`), add calls to `self.rate_limiter.retain_recent()` and `self.forward_rate_limiter.retain_recent()` on every `CHECK_INTERVAL` tick, not only on disconnect.
2. **Validate `from` against the sending session's authenticated peer ID**: Before calling `check_key`, reject messages where `content.from` does not match the actual peer ID of the sending session. This prevents spoofing of `from = local_peer_id` entirely.
3. **Cap the `forward_rate_limiter` map size**: Reject new entries when the map exceeds a configurable bound (e.g., 10,000 entries), or switch to a bounded LRU-backed state store.

## Proof of Concept

```
1. Attacker establishes a P2P connection to the victim node.
2. Attacker learns victim's local_peer_id via the identify protocol.
3. Attacker sends up to 30 ConnectionRequestDelivered messages per second:
     from         = local_peer_id      (spoofed as victim's own peer ID)
     route        = []                 (empty)
     to           = random_peer_id_i   (unique per message, e.g., random 39-byte PeerId)
     listen_addrs = [any valid addr with 1..=24 entries]
4. Per message execution path:
   a. Outer rate_limiter passes (first 30/sec for this session × item_id).
   b. forward_rate_limiter.check_key((local_peer_id, random_peer_id_i, ITEM_ID))
      → inserts new HashMap entry (key never seen before), check passes.
   c. route.last() == None → enters terminal branch.
   d. self_peer_id == content.from → skips forward path.
   e. inflight_requests.remove(random_peer_id_i) → None.
   f. Returns StatusCode::Ignore(501) → no ban.
5. After N messages, forward_rate_limiter HashMap contains N entries.
6. notify() fires every 5 minutes but does NOT call retain_recent() → no eviction.
7. Entries accumulate until disconnect; with multiple sessions, growth is linear.
8. Assertion: map size grows monotonically at 30 entries/sec/session with no eviction
   and no ban until memory is exhausted.
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

**File:** network/src/protocols/hole_punching/mod.rs (L169-176)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
        let status = self.network_state.connection_status();

        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);

```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L134-145)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
        {
            debug!(
                "from: {}, to {}, item_name: {}, rate limit is reached",
                content.from, content.to, "ConnectionRequestDelivered",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionRequestDelivered");
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L147-176)
```rust
        match content.route.last() {
            Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
            None => {
                let self_peer_id = self.protocol.network_state.local_peer_id();
                if self_peer_id != &content.from {
                    // forward the message to the `from` peer
                    self.forward_delivered(&content.from).await
                } else {
                    // the current peer is the target peer, respond the sync back
                    if let Some(metrics) = ckb_metrics::handle() {
                        metrics.ckb_hole_punching_active_count.inc();
                    }

                    let request_start = self.protocol.inflight_requests.remove(&content.to);

                    match request_start {
                        Some(start) => {
                            let res = self.respond_sync(content.from).await;
                            if !res.is_ok() {
                                return res;
                            }
                            let now = unix_time_as_millis();
                            let ttl = now - start;

                            self.try_nat_traversal(ttl, content.listen_addrs);

                            Status::ok()
                        }
                        None => StatusCode::Ignore.with_context("the request is not in flight"),
                    }
```

**File:** network/src/protocols/hole_punching/status.rs (L99-106)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        let code = self.code() as u16;
        if (400..500).contains(&code) {
            Some(BAD_MESSAGE_BAN_TIME)
        } else {
            None
        }
    }
```
