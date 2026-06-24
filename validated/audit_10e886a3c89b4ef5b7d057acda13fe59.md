The code confirms all claims. Let me verify the key facts:

1. **`forward_rate_limiter` type** — confirmed `RateLimiter<(PeerId, PeerId, u32)>` backed by `HashMapStateStore` [1](#0-0) 

2. **`retain_recent()` only in `disconnected()`** — confirmed, lines 67–68 [2](#0-1) 

3. **`notify()` has no `retain_recent()` call** — confirmed, only `pending_delivered` and `inflight_requests` are cleaned [3](#0-2) 

4. **Outer rate limiter does not bound inner HashMap** — outer limiter is keyed by `(PeerIndex, u32)` at 30/sec; each of those 30 messages can carry a fresh unique `(from, to)` PeerId pair, inserting a new entry into `forward_rate_limiter` [4](#0-3) 

5. **No validation that `from` matches the actual sender** — `from` is taken directly from message content without checking it equals the session's real PeerId, so arbitrary PeerIds are accepted [5](#0-4) 

All five validation checks pass. The vulnerability is real and exploitable.

---

Audit Report

## Title
Unbounded Memory Growth via Unique PeerId Pairs in `forward_rate_limiter` — (File: `network/src/protocols/hole_punching/mod.rs`)

## Summary
The `HolePunching` protocol's `forward_rate_limiter` is a `HashMapStateStore`-backed keyed rate limiter whose only cleanup call, `retain_recent()`, is placed exclusively in `disconnected()`. An attacker maintaining a persistent session can insert up to 30 new `(PeerId, PeerId, u32)` entries per second into the HashMap indefinitely, causing unbounded memory growth and eventual OOM crash of the CKB node.

## Finding Description
`forward_rate_limiter` is declared as `RateLimiter<(PeerId, PeerId, u32)>` backed by `governor::state::keyed::HashMapStateStore`, which stores one entry per unique key and never self-evicts. The only cleanup is `retain_recent()` called in `disconnected()` (lines 67–68). The periodic `notify()` callback (every 5 minutes, lines 169–175) cleans `pending_delivered` and `inflight_requests` but never calls `retain_recent()` on either rate limiter.

The outer per-session rate limiter (30/sec keyed by `(PeerIndex, u32)`) does not bound the inner HashMap size — it only limits the insertion rate. Each of the 30 allowed `ConnectionRequest` messages per second can carry a fresh unique `(from, to)` PeerId pair (lines 132–143). The `from` and `to` fields are parsed directly from attacker-controlled message content with no check that `from` matches the actual session's peer ID (lines 111–135), so arbitrary PeerIds are accepted. With a persistent connection, `disconnected()` is never called, so `retain_recent()` is never invoked, and the HashMap accumulates entries indefinitely.

## Impact Explanation
**High** — *Vulnerabilities which could easily crash a CKB node.* At 30 insertions/sec with ~150–200 bytes per `(PeerId, PeerId, u32)` entry in `hashbrown`, memory grows at ~5 KB/sec. Over 24 hours this reaches ~390–520 MB; over a week ~2.7–3.6 GB, causing process OOM. This crashes block validation, transaction relay, and all P2P activity on the victim node.

## Likelihood Explanation
The attacker requires only a single persistent inbound or outbound P2P connection to the victim node — trivially achievable on mainnet. No PoW, no privileged role, and no key material is required. The HolePunching protocol is enabled by default when `SupportProtocol::HolePunching` is in the node config. The attack is repeatable and requires no victim interaction beyond accepting the connection.

## Recommendation
Add `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside the `notify()` callback so stale entries are evicted every 5 minutes regardless of session lifetime. Additionally, consider validating that the `from` field in `ConnectionRequest` matches the actual session's peer ID to prevent injection of arbitrary PeerIds.

## Proof of Concept
1. Connect one session to the victim node on the HolePunching protocol.
2. In a loop at 30 msg/sec, send `ConnectionRequest` messages where `from` and `to` are freshly generated random valid `PeerId` values each iteration.
3. The outer `rate_limiter` passes each message (30/sec budget not exceeded per `(session_id, item_id)`).
4. `forward_rate_limiter.check_key(&(from, to, item_id))` inserts a new `HashMapStateStore` entry for each unique pair (first-time keys always pass the 1/sec quota).
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L111-143)
```rust
        let content = match RequestContent::try_from(&self.message) {
            Ok(content) => content,
            Err(status) => return status,
        };
        if content.listen_addrs.len() > ADDRS_COUNT_LIMIT || content.listen_addrs.is_empty() {
            return StatusCode::InvalidListenAddrLen
                .with_context("the listen address count is too large or empty");
        }

        if content.max_hops > MAX_HOPS {
            return StatusCode::InvalidMaxTTL.into();
        }
        if content.route.len() > MAX_HOPS as usize {
            return StatusCode::InvalidRoute.with_context("the route length is too long");
        }

        let self_peer_id = self.protocol.network_state.local_peer_id();
        if content.route.contains(self_peer_id) {
            return StatusCode::Ignore.with_context("the message is passed, ignore it");
        }

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
