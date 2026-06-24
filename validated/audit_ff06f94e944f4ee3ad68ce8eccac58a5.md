The code confirms all claims in the report. Let me verify the key assertions:

**Claim 1: `forward_rate_limiter` uses `HashMapStateStore` keyed by `(PeerId, PeerId, u32)`**
Confirmed at lines 31–46 of `mod.rs`. [1](#0-0) 

**Claim 2: `retain_recent()` only called in `disconnected()`**
Confirmed at lines 66–70. [2](#0-1) 

**Claim 3: `notify()` never calls `retain_recent()`**
Confirmed at lines 169–175 — only `pending_delivered` and `inflight_requests` are cleaned; neither rate limiter is pruned. [3](#0-2) 

**Claim 4: Outer rate limiter doesn't bound inner HashMap size**
The outer `rate_limiter` is keyed by `(session_id, msg.item_id())` at 30/sec. Each of those 30 allowed messages can carry a distinct `(from, to)` PeerId pair, each inserting a new entry into `forward_rate_limiter`. The `forward_rate_limiter` quota is 1/sec per unique `(from, to, item_id)` — it rate-limits *repeat* pairs, not *unique* pairs. [4](#0-3) 

**No additional guards prevent this:** The `route.contains(self_peer_id)` check (line 128) is trivially bypassed by omitting the victim's peer ID from the route. PeerId parsing (lines 36–40) only rejects malformed bytes, not attacker-controlled valid PeerIds. [5](#0-4) 

All five required validation checks pass. The finding is valid.

---

Audit Report

## Title
Unbounded Memory Growth via Unique PeerId Pairs in `forward_rate_limiter` — (File: `network/src/protocols/hole_punching/mod.rs`)

## Summary
The `HolePunching` protocol's `forward_rate_limiter` is a `HashMapStateStore`-backed `RateLimiter<(PeerId, PeerId, u32)>` that accumulates one entry per unique `(from, to, item_id)` key and never self-evicts. The only cleanup call, `retain_recent()`, is placed exclusively in `disconnected()`. A single persistent attacker session can insert up to 30 new unique entries per second indefinitely, causing unbounded RSS growth and eventual OOM crash of the CKB node.

## Finding Description
`forward_rate_limiter` is declared as `RateLimiter<(PeerId, PeerId, u32)>` backed by `governor::state::keyed::HashMapStateStore`, which stores one entry per unique key and never self-evicts:

```rust
// mod.rs L31-46
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;
...
forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```

`retain_recent()` is called only in `disconnected()` (lines 66–70). The periodic `notify()` callback (every 5 minutes, lines 169–175) cleans `pending_delivered` and `inflight_requests` but never calls `retain_recent()` on either rate limiter.

The outer per-session rate limiter (30/sec keyed by `(session_id, msg.item_id())`) does not bound the inner HashMap size — it only limits the insertion rate. Each of the 30 allowed messages/sec can carry a fresh unique `(from, to)` PeerId pair (attacker-controlled fields parsed at `connection_request.rs` lines 36–40), inserting a new entry into `forward_rate_limiter` at line 135:

```rust
self.protocol.forward_rate_limiter
    .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
```

The `route.contains(self_peer_id)` guard (line 128) is trivially bypassed by omitting the victim's peer ID from the route field. With a persistent connection, `disconnected()` is never called, so `retain_recent()` is never invoked, and the HashMap accumulates entries indefinitely.

## Impact Explanation
**High severity** — matches *"Vulnerabilities which could easily crash a CKB node"* (10001–15000 points). At 30 insertions/sec with ~150–200 bytes per `(PeerId, PeerId, u32)` entry in `hashbrown`, memory grows at ~5 KB/sec. Over 24 hours this reaches ~390–520 MB; over a week ~2.7–3.6 GB, causing process OOM. This crashes block validation, transaction relay, and all P2P activity on the victim node.

## Likelihood Explanation
The attacker requires only a single persistent inbound or outbound P2P connection to the victim node — trivially achievable on mainnet. No PoW, no privileged role, and no key material is required. The HolePunching protocol is enabled by default when `SupportProtocol::HolePunching` is in the node config. The attack is repeatable and requires no victim interaction beyond accepting the connection.

## Recommendation
Add `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside the `notify()` callback so stale entries are evicted every 5 minutes regardless of session lifetime:

```rust
async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
    let status = self.network_state.connection_status();
    let now = unix_time_as_millis();
    self.pending_delivered.retain(|_, (_, t)| (now - *t) < TIMEOUT);
    self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
    // Add these two lines:
    self.rate_limiter.retain_recent();
    self.forward_rate_limiter.retain_recent();
    // ... rest of notify
}
```

## Proof of Concept
1. Connect one session to the victim node on the HolePunching protocol.
2. In a loop at 30 msg/sec, send `ConnectionRequest` messages where `from` and `to` are freshly generated random valid `PeerId` bytes each iteration, with a non-empty `listen_addrs`, `max_hops ≤ 6`, and a `route` that does not contain the victim's peer ID.
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L127-130)
```rust
        let self_peer_id = self.protocol.network_state.local_peer_id();
        if content.route.contains(self_peer_id) {
            return StatusCode::Ignore.with_context("the message is passed, ignore it");
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
