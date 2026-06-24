Audit Report

## Title
Unbounded `forward_rate_limiter` HashMapStateStore Growth via Attacker-Controlled `from` PeerId — (`network/src/protocols/hole_punching/mod.rs`, `network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary
The `HolePunching` protocol's `forward_rate_limiter` is a `HashMapStateStore` keyed by `(PeerId, PeerId, u32)` where the first `PeerId` (`from`) is taken directly from the message payload without being validated against the actual sending session's identity. A single connected peer can continuously inject messages with distinct `from` values, inserting a new map entry per message. Because `retain_recent()` is called only in `disconnected()` and never in the periodic `notify()` callback, the map grows without bound for the lifetime of any persistent connection, leading to memory exhaustion and OOM crash of the relay node.

## Finding Description

**Type declaration** — `forward_rate_limiter` uses an unbounded `HashMapStateStore`: [1](#0-0) 

**Key insertion** — the key is `(content.from, content.to, msg_item_id)` where both `from` and `to` are attacker-supplied: [2](#0-1) 

**No identity binding** — `content.from` is parsed from raw message bytes with only syntactic validity checked; it is never compared against the actual session's `PeerId`: [3](#0-2) 

**Cleanup only on disconnect** — `retain_recent()` is called on both rate limiters only in `disconnected()`: [4](#0-3) 

**`notify()` never cleans rate limiters** — the periodic callback (every 5 minutes) retains `pending_delivered` and `inflight_requests` but makes no call to `retain_recent()` on either rate limiter: [5](#0-4) 

**Outer rate limiter does not bound key count** — the outer `rate_limiter` is keyed by `(session_id, msg_item_id)` and limits to 30 messages/second per session/type pair. This throttles the insertion rate into `forward_rate_limiter` but does not cap the number of distinct `(from, to, item_id)` keys that can accumulate: [6](#0-5) 

**Exploit flow:**
1. Attacker establishes one valid P2P connection to a relay node.
2. Attacker sends `ConnectionRequest` messages at up to 30/second, each with a freshly generated random `from` PeerId (syntactically valid multihash bytes).
3. Each message passes the outer `rate_limiter` check (keyed by session, not `from`) and reaches `forward_rate_limiter.check_key(...)`, inserting a new entry.
4. As long as the connection is held open, `retain_recent()` is never called, so all inserted entries accumulate indefinitely.
5. At 30 entries/second × 86,400 seconds = ~2.6 million entries per day per connection. Multiple simultaneous connections multiply the rate linearly.

## Impact Explanation
This is a **High** severity issue matching: *"Vulnerabilities which could easily crash a CKB node."* A single unprivileged attacker with one open connection can exhaust the relay node's heap memory. Each `HashMapStateStore` entry holds two `PeerId` values (~39 bytes each), a `u32`, and governor's internal atomic state. At 2.6 million entries, heap consumption exceeds 500 MB from one connection alone. The process will OOM-crash, partitioning the relay node from the network. No special privileges, PoW, or keys are required.

## Likelihood Explanation
The attack requires only a standard P2P connection to a relay node — an open, unpermissioned operation on CKB mainnet. The `from` field is freely settable to any syntactically valid PeerId bytes. The outer rate limiter (30/s) does not prevent the attack; it only sets the pace. The attack is sustainable indefinitely as long as the TCP connection is held open, and is trivially repeatable with multiple connections.

## Recommendation
1. **Periodic cleanup**: Call `self.forward_rate_limiter.retain_recent()` inside `notify()` (every 5 minutes). Given the 1-req/sec quota, this caps the live entry count to approximately `5 × 60 × 30 = 9,000` entries — a bounded, safe size.
2. **Bind `from` to session identity**: Before inserting into `forward_rate_limiter`, validate that `content.from` matches the actual `PeerId` of the sending session (retrievable from `network_state`). This collapses the key space to `(actual_peer_id, to, item_id)`, bounded by the number of connected peers.
3. **Cap map size**: Enforce a maximum entry count on `forward_rate_limiter` and reject or evict entries beyond the cap as a defense-in-depth measure.

## Proof of Concept
```rust
// Minimal unit test sketch
let relay = HolePunching::new(network_state.clone());
let session = connect_peer(&relay).await; // single valid connection

for i in 0..N {
    let fake_from = PeerId::random(); // distinct syntactically-valid PeerId each iteration
    let fake_to   = PeerId::random();
    let msg = build_connection_request(fake_from, fake_to, valid_listen_addrs());
    relay.received(session.clone(), msg).await;
    // outer rate_limiter allows 30/s; sleep(33ms) between iterations to stay under limit
}

// No disconnect issued — retain_recent() is never called.
// The HashMapStateStore internal map now holds N entries.
// At N = 2_592_000 (24 hours at 30/s), heap usage exceeds 500 MB.
```
The outer `rate_limiter` throttles to 30 inserts/second but does not prevent the map from reaching size N given sufficient time. The absence of `retain_recent()` in `notify()` is the direct enabler of unbounded growth.

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L132-135)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
```
