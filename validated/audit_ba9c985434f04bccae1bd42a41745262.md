The code confirms all claims. Let me verify the governor library behavior and the rate limiter quota to ensure the math holds.

All claims verified against the actual code. The `retain_recent()` grep confirms it is called only inside `disconnected()` in the hole-punching module — never in `notify()`.

Audit Report

## Title
Unbounded `forward_rate_limiter` HashMap Growth via Attacker-Controlled PeerId Keys — (`network/src/protocols/hole_punching/mod.rs`)

## Summary
The `HolePunching` protocol's `forward_rate_limiter` is a `governor::RateLimiter` backed by a `HashMapStateStore<(PeerId, PeerId, u32)>`. Its keys are derived from attacker-controlled `from`/`to` fields in `ConnectionSync` wire messages. The only cleanup call — `retain_recent()` — is invoked exclusively in `disconnected()`. The periodic `notify()` timer never cleans the rate limiter store. A persistent peer can therefore grow the `forward_rate_limiter` HashMap without bound, exhausting process memory and crashing the node.

## Finding Description
`forward_rate_limiter` is declared as `RateLimiter<(PeerId, PeerId, u32)>` backed by `governor::state::keyed::HashMapStateStore`. [1](#0-0) 

In `ConnectionSyncProcess::execute()`, the key inserted into this store is built from `content.from` and `content.to`, which are parsed directly from the wire message bytes — fully attacker-controlled values, not the session identity. [2](#0-1) [3](#0-2) 

`retain_recent()` is called on both rate limiters only inside `disconnected()`: [4](#0-3) 

The 5-minute periodic `notify()` timer cleans only `pending_delivered` and `inflight_requests` — there is no call to `forward_rate_limiter.retain_recent()`: [5](#0-4) 

The outer `rate_limiter` is keyed by `(session_id, msg.item_id())` and allows 30 messages/second per session: [6](#0-5) 

This throttles message throughput to 30/sec, but each of those 30 messages can carry a distinct `(from, to)` pair, inserting 30 new entries/second into `forward_rate_limiter`. The outer limiter bounds rate, not key-space cardinality. The governor `HashMapStateStore` does not self-evict; `retain_recent()` is the only removal mechanism, and it is never called during a live connection.

## Impact Explanation
This is a **High** severity finding: **Vulnerabilities which could easily crash a CKB node**.

At 30 entries/second sustained over a long-lived connection, the HashMap grows at ~6 KB/sec (each entry holds two `PeerId` values ~39 bytes each, a `u32`, and HashMap overhead). After 24 hours this is ~2.6 million entries consuming hundreds of MB. A node with multiple persistent attacker-controlled peers is multiplied accordingly. The process will be OOM-killed by the OS, crashing the node.

## Likelihood Explanation
The attack requires only a standard P2P connection — no privilege, no proof-of-work, no key material. The attacker generates synthetic PeerId byte strings (any valid multihash bytes accepted by `PeerId::from_bytes`) and sends them as `from`/`to` fields in `ConnectionSync` messages. The outer rate limiter slows but does not stop the attack. The connection can be maintained indefinitely without triggering a ban, since `TooManyRequests` on the forward limiter does not result in a ban (only a debug log and early return). [3](#0-2) 

## Recommendation
Add `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside the `notify()` handler, which already fires every 5 minutes. This mirrors the pattern already used in `disconnected()` and ensures stale entries (those whose 1-second quota window has long expired) are periodically evicted regardless of whether any peer disconnects. [5](#0-4) 

## Proof of Concept
1. Connect to a CKB node as a normal P2P peer on the HolePunching protocol.
2. In a loop at ≤30 msg/sec, send `ConnectionSync` messages where `from` and `to` are randomly generated valid PeerId bytes (e.g., 39-byte multihash values), and `route` is empty (passes the `MAX_HOPS` check).
3. Each message passes the outer `rate_limiter` (keyed by `session_id`, fixed `item_id`) and inserts a new unique key into `forward_rate_limiter`.
4. Maintain the connection without disconnecting (never triggering `retain_recent()`).
5. Observe process RSS growing at ~30 entries/sec × ~200 bytes/entry ≈ 6 KB/sec.
6. After hours/days, the node process is OOM-killed.
7. Verification: assert `forward_rate_limiter` key count equals N after N distinct `(from, to)` messages; assert it drops to near-zero after disconnect (when `retain_recent()` removes expired entries).

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
