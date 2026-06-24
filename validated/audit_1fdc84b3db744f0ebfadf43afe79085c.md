The code matches all cited references. Let me verify the key claims:

**Claim 1: `forward_rate_limiter` keyed by `(PeerId, PeerId, u32)`** — Confirmed at [1](#0-0) 

**Claim 2: `from` is wire-controlled, only syntactically validated** — Confirmed at [2](#0-1)  No check that `content.from` equals the actual sender's peer ID.

**Claim 3: `check_key` uses attacker-controlled `content.from`** — Confirmed at [3](#0-2) 

**Claim 4: `retain_recent()` only in `disconnected`, not in `notify`** — Confirmed. `disconnected` calls it at [4](#0-3)  but `notify` only cleans `pending_delivered` and `inflight_requests` at [5](#0-4)  — `forward_rate_limiter.retain_recent()` is absent.

**Claim 5: Outer rate limiter is 30/sec per `(session_id, msg_item_id)`** — Confirmed at [6](#0-5)  The `forward_rate_limiter` quota is 1/sec per `(from, to, item_id)` at [7](#0-6)  — each unique `from` creates a fresh bucket that always passes.

All code references are exact and the exploit path is coherent. The vulnerability is valid.

---

Audit Report

## Title
Unbounded `forward_rate_limiter` HashMap Growth via Spoofed `from` PeerId in `ConnectionRequest` — (File: `network/src/protocols/hole_punching/mod.rs`, `network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary
The `forward_rate_limiter` in `HolePunching` uses a `HashMapStateStore` keyed by `(PeerId, PeerId, u32)`, where the `from` field is taken directly from the wire message with no validation that it matches the actual sender's peer ID. An attacker holding a single persistent connection can send 30 `ConnectionRequest` messages per second (the outer rate limit), each with a distinct spoofed `from` PeerId, creating 30 new HashMap buckets per second. Because `retain_recent()` is never called in the periodic `notify` callback, the map grows without bound for the lifetime of the connection, enabling a memory-exhaustion crash of the target node.

## Finding Description
`forward_rate_limiter` is declared as `RateLimiter<(PeerId, PeerId, u32)>` backed by `governor::state::keyed::HashMapStateStore`. In `ConnectionRequestProcess::execute`, the key inserted is `(content.from.clone(), content.to.clone(), self.msg_item_id)`. `content.from` is parsed from the wire message's `from` bytes field with only syntactic validation (`PeerId::from_bytes`); there is no check that it equals the peer ID of the actual sending session. An attacker can therefore set `from` to any valid PeerId bytes.

The outer `rate_limiter` (keyed by `(session_id, msg_item_id)`) allows 30 `ConnectionRequest` messages per second per session. Each of those 30 messages can carry a distinct `from` PeerId, creating a fresh bucket in `forward_rate_limiter` that has never been seen before — so the 1/sec forward rate limit check always passes for new keys, and a new entry is inserted every time.

`retain_recent()` is called on `forward_rate_limiter` only inside `disconnected`. The `notify` callback (fired every 5 minutes) cleans `pending_delivered` and `inflight_requests` but never calls `forward_rate_limiter.retain_recent()`. As long as the attacker maintains the connection, no eviction occurs and the HashMap grows indefinitely.

## Impact Explanation
Each `HashMapStateStore` entry holds two `PeerId` values (~39 bytes each), a `u32`, and governor's internal rate state, totalling roughly 150–300 bytes per entry. At 30 entries/sec per connection, with a typical maximum of ~125 connections all controlled by the attacker, the growth rate is ~3,750 entries/sec ≈ ~1 MB/sec. A relay node with 4 GB of available heap would be exhausted in under an hour of sustained attack, causing an OOM crash. This matches the allowed CKB bounty impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation
The attack requires only the ability to open a TCP connection to the target node and send well-formed `ConnectionRequest` messages with varying `from` bytes. No special privilege, proof-of-work, or key material is needed. The outer rate limiter (30/sec) is not a meaningful defense against a long-lived connection — it merely sets the growth rate. The attack is repeatable, persistent, and requires no victim interaction.

## Recommendation
1. **Periodic cleanup**: Call `self.forward_rate_limiter.retain_recent()` inside the `notify` callback alongside the existing `pending_delivered` and `inflight_requests` cleanup.
2. **Bind `from` to the actual sender**: Before inserting into `forward_rate_limiter`, validate that `content.from` equals the peer ID of the actual sending session (resolvable from `context.session.id` via the peer registry). This eliminates the spoofing vector entirely.
3. **Cap the HashMap size**: Add a hard upper bound on the number of entries in `forward_rate_limiter` and drop/reject messages when the cap is reached.

## Proof of Concept
```rust
// Pseudocode unit test
let mut protocol = HolePunching::new(network_state);
// Outer rate limiter allows 30/sec; send one per iteration with unique `from`
for i in 0..N {
    let fake_from = PeerId::random(); // distinct each iteration
    let msg = build_connection_request(fake_from, some_to, valid_listen_addr);
    protocol.received(ctx, msg).await;
    // Each call: outer rate_limiter passes (same session/item_id, ≤30/sec)
    // forward_rate_limiter: new (fake_from, some_to, item_id) bucket → always passes
    // → new HashMap entry inserted, never evicted (no disconnect)
}
// forward_rate_limiter internal map now has N entries
assert_eq!(forward_rate_limiter_size(&protocol), N);
```
Manual steps: connect to a CKB node, send 30 `ConnectionRequest` messages per second with `from` set to `PeerId::random()` bytes each time, sustain for hours. Monitor RSS growth on the target process; no disconnect means `retain_recent()` is never called.

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

**File:** network/src/protocols/hole_punching/mod.rs (L66-69)
```rust
    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.rate_limiter.retain_recent();
        self.forward_rate_limiter.retain_recent();
        debug!("HolePunching.disconnected session={}", context.session.id);
```

**File:** network/src/protocols/hole_punching/mod.rs (L173-175)
```rust
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/mod.rs (L251-252)
```rust
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/hole_punching/mod.rs (L256-257)
```rust
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
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
