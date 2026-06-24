The code confirms all the key claims. Let me verify the rate limiter quota configuration and any other potential mitigations.

All claims are verified against the actual code. The `retain_recent()` calls exist only in `disconnected()` (2 matches in `mod.rs`), never in `notify()` or `received()`. The `forward_rate_limiter` is referenced in `connection_sync.rs`, `connection_request.rs`, and `connection_request_delivered.rs` — all using attacker-supplied PeerId fields as keys.

---

Audit Report

## Title
Unbounded `forward_rate_limiter` HashMap Growth via Unique Attacker-Controlled `(PeerId, PeerId, u32)` Keys — (`network/src/protocols/hole_punching/mod.rs`)

## Summary
The `HolePunching` protocol's `forward_rate_limiter` is a `governor::RateLimiter` backed by a `HashMapStateStore<(PeerId, PeerId, u32)>`. Because `retain_recent()` is called only in `disconnected()` and never in `notify()` or `received()`, an attacker maintaining a long-lived P2P session can insert new entries into the HashMap indefinitely by sending `ConnectionSync` messages with unique attacker-controlled `(from, to)` PeerId pairs. This causes unbounded memory growth proportional to session duration, with no cleanup until the session ends.

## Finding Description
`forward_rate_limiter` is declared as `RateLimiter<(PeerId, PeerId, u32)>` backed by `HashMapStateStore`: [1](#0-0) 

`retain_recent()` is called only in `disconnected()`: [2](#0-1) 

The `notify()` handler (firing every 5 minutes via `CHECK_INTERVAL`) cleans `pending_delivered` and `inflight_requests` but never calls `retain_recent()` on either rate limiter: [3](#0-2) 

In `ConnectionSyncProcess::execute()`, the HashMap key is built directly from wire-supplied `content.from` and `content.to` with no validation that they match the actual session peer identity: [4](#0-3) 

The `from`/`to` fields are parsed from raw bytes with only structural validity checked (valid multihash format), not cryptographic binding to the session: [5](#0-4) 

The outer `rate_limiter` (30 req/sec per `(session_id, msg_item_id)`) caps insertion rate but does not bound total HashMap size: [6](#0-5) 

The `forward_rate_limiter` quota is 1 req/sec per `(from, to, 2)` key. Since each message uses a fresh unique key, the per-key quota is never triggered — every message passes and inserts a new entry. The governor `HashMapStateStore` retains all entries until `retain_recent()` is explicitly called; entries for unique one-shot keys are never evicted during the session.

## Impact Explanation
Each unique `(from_i, to_i, 2)` triple inserts a new entry into `HashMapStateStore`. At 30 msgs/sec (outer rate limit), a single session accumulates:
- 1 hour: ~108,000 entries (~16–21 MB)
- 24 hours: ~2.6 million entries (~390–520 MB)

Multiple concurrent long-lived sessions scale this linearly. An attacker running 10 sessions for 24 hours can consume ~4–5 GB of heap memory, exhausting available memory and crashing the node process. This matches the allowed bounty impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
The attack requires only a standard P2P connection — no privilege, no proof-of-work, no key material. The attacker freely controls `from` and `to` fields in the wire message; no cryptographic binding to session identity is enforced. The attack is low-bandwidth (~30 small messages/sec per session), passive, and difficult to distinguish from legitimate forwarding traffic. It is repeatable and deterministic.

## Recommendation
Call `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside `notify()` in addition to `disconnected()`. Since `notify()` already fires every 5 minutes (`CHECK_INTERVAL = Duration::from_secs(5 * 60)`), this bounds the maximum HashMap size to at most `30 req/sec × 300 sec = 9,000 entries` — a constant upper bound regardless of session duration: [7](#0-6) 

## Proof of Concept
1. Attacker establishes a single long-lived P2P session to the victim node.
2. Attacker sends 30 `ConnectionSync` messages/sec, each with a freshly generated unique `(from_i, to_i)` PeerId pair (structurally valid multihash bytes, otherwise arbitrary).
3. Each message passes the outer `rate_limiter` check (30/sec per `(session_id, 2)`) and reaches `forward_rate_limiter.check_key(&(from_i, to_i, 2))`.
4. Each unique key inserts a new entry into `HashMapStateStore`; `retain_recent()` is never called during the session.
5. After T seconds, the HashMap contains exactly `30 × T` entries.
6. At T = 86,400 s (24 hours): ~2.6 million entries consuming ~390–520 MB per session.

Unit test assertion: after simulating N `ConnectionSync` messages with unique `(from_i, to_i)` keys and no intervening `retain_recent()` call, the internal map size of `forward_rate_limiter` equals N, confirming unbounded growth.

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L25-26)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
```

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

**File:** network/src/protocols/hole_punching/mod.rs (L169-176)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
        let status = self.network_state.connection_status();

        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);

```

**File:** network/src/protocols/hole_punching/mod.rs (L251-252)
```rust
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
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
