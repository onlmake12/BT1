Audit Report

## Title
Unbounded Memory Growth via Unique PeerId Pairs in `forward_rate_limiter` — (File: `network/src/protocols/hole_punching/mod.rs`)

## Summary
The `HolePunching` protocol's `forward_rate_limiter` uses a `HashMapStateStore` keyed by attacker-controlled `(PeerId, PeerId, u32)` tuples. The only cleanup call, `retain_recent()`, is placed exclusively in `disconnected()`. As long as an attacker maintains a persistent session, the HashMap grows without bound at up to 30 entries/second, eventually causing an OOM crash of the CKB node.

## Finding Description
`forward_rate_limiter` is declared as `RateLimiter<(PeerId, PeerId, u32)>` backed by `governor::state::keyed::HashMapStateStore`, which stores one entry per unique key and never self-evicts: [1](#0-0) 

`retain_recent()` is called only in `disconnected()`: [2](#0-1) 

The periodic `notify()` callback (every 5 minutes) cleans `pending_delivered` and `inflight_requests` but never calls `retain_recent()` on either rate limiter: [3](#0-2) 

The outer per-session rate limiter (30/sec keyed by `(session_id, msg.item_id())`) does not bound the inner HashMap size — it only limits the insertion rate. Each of the 30 allowed messages/sec can carry a fresh unique `(from, to)` PeerId pair, inserting a new entry into `forward_rate_limiter`: [4](#0-3) 

With a persistent connection, `disconnected()` is never called, so `retain_recent()` is never invoked, and the HashMap accumulates entries indefinitely.

## Impact Explanation
This is a **High** severity issue matching the allowed impact: *"Vulnerabilities which could easily crash a CKB node."* At 30 insertions/sec with ~150–200 bytes per `(PeerId, PeerId, u32)` entry in `hashbrown`, memory grows at ~5 KB/sec. Over 24 hours this reaches ~390–520 MB; over a week ~2.7–3.6 GB, causing process OOM. This crashes block validation, transaction relay, and all P2P activity on the victim node.

## Likelihood Explanation
The attacker requires only a single persistent inbound or outbound P2P connection to the victim node — trivially achievable on mainnet. No PoW, no privileged role, and no key material is required. The HolePunching protocol is enabled by default when `SupportProtocol::HolePunching` is in the node config: [5](#0-4) 

The attack is repeatable and requires no victim interaction beyond accepting the connection.

## Recommendation
Add `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside the `notify()` callback so stale entries are evicted every 5 minutes regardless of session lifetime: [3](#0-2) 

## Proof of Concept
1. Connect one session to the victim node on the HolePunching protocol.
2. In a loop at 30 msg/sec, send `ConnectionRequest` messages where `from` and `to` are freshly generated random `PeerId` values each iteration.
3. The outer `rate_limiter` passes each message (30/sec budget not exceeded per `(session_id, item_id)`).
4. `forward_rate_limiter.check_key(&(from, to, item_id))` inserts a new `HashMapStateStore` entry for each unique pair.
5. Never disconnect; `retain_recent()` is never called.
6. Monitor victim RSS: it grows at ~5 KB/sec. After ~24 hours the node OOMs and crashes.

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

**File:** network/src/network.rs (L940-953)
```rust
        // HolePunching protocol
        #[cfg(not(target_family = "wasm"))]
        if config
            .support_protocols
            .contains(&SupportProtocol::HolePunching)
        {
            let hole_punching_state = Arc::clone(&network_state);
            let hole_punching_meta =
                SupportProtocols::HolePunching.build_meta_with_service_handle(move || {
                    ProtocolHandle::Callback(Box::new(
                        crate::protocols::hole_punching::HolePunching::new(hole_punching_state),
                    ))
                });
            protocol_metas.push(hole_punching_meta);
```
