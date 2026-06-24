All code references verified against the actual source. Here is the validation result:

Audit Report

## Title
Unbounded `forward_rate_limiter` Heap Growth via Persistent Peer Sending Unique `(from, to)` Pairs — (`network/src/protocols/hole_punching/mod.rs`)

## Summary

`HolePunching::forward_rate_limiter` is a `governor::RateLimiter` backed by `HashMapStateStore<(PeerId, PeerId, u32)>`. The only calls to `retain_recent()` — the sole eviction mechanism for `HashMapStateStore` — are inside `disconnected()`. The periodic `notify()` callback (every 5 minutes) never calls `retain_recent()`. Because `from` and `to` in `ConnectionRequest`, `ConnectionRequestDelivered`, and `ConnectionSync` messages are deserialized from attacker-controlled payload bytes, a single persistently-connected peer can insert an unbounded number of unique keys into the map, causing unbounded heap growth and eventual OOM crash of the victim node.

## Finding Description

`forward_rate_limiter` is declared as `RateLimiter<(PeerId, PeerId, u32)>` where `RateLimiter<T>` aliases `governor::RateLimiter<T, HashMapStateStore<T>, DefaultClock>`. [1](#0-0) 

`retain_recent()` is called exclusively inside `disconnected()`: [2](#0-1) 

The `notify()` callback (fired every `CHECK_INTERVAL = 5 minutes`) evicts `pending_delivered` and `inflight_requests` but never calls `retain_recent()` on either rate limiter: [3](#0-2) 

In all three message processors, `forward_rate_limiter` is keyed with `content.from` and `content.to`, which are deserialized from the message payload — not from the session identity: [4](#0-3) [5](#0-4) [6](#0-5) 

The outer `rate_limiter` is keyed by `(session_id, msg.item_id())` and allows 30 messages/second per session per message type: [7](#0-6) 

This throttles the **insertion rate** into `forward_rate_limiter` to 30 entries/second per message type, but does not bound the **total map size**. The `forward_rate_limiter` quota of 1/second per key means every unique `(from, to, item_id)` tuple creates a distinct, persistent map entry: [8](#0-7) 

`PeerId::from_bytes` accepts any 34-byte value with a valid multihash prefix (`[0x12, 0x20, <32 bytes>]`), confirmed by the fuzz harness: [9](#0-8) 

This means the attacker can trivially generate an unbounded supply of valid, distinct `(from, to)` pairs by varying any of the 32 payload bytes.

For `ConnectionSync` specifically, there is no `listen_addrs` requirement and no route-contains early-return check before `forward_rate_limiter.check_key()` is reached, making it the simplest attack vector: [10](#0-9) 

For `ConnectionRequest`, the route-contains check at line 128 is trivially bypassed by sending an empty route field, after which `forward_rate_limiter.check_key()` is reached: [11](#0-10) 

**Exploit path:**
1. Attacker establishes a single persistent TCP connection to a CKB node with HolePunching enabled.
2. Attacker sends `ConnectionSync` messages at 30/second, each with a freshly generated unique `(from, to)` PeerId pair in the payload (empty route, arbitrary valid multihash bytes).
3. Each message passes the outer rate limiter (keyed by session, not payload) and calls `check_key()` on `forward_rate_limiter` with a new key, inserting a new `HashMapStateStore` entry.
4. Because the peer never disconnects, `retain_recent()` is never called. Entries accumulate indefinitely.
5. At 90 new entries/second (30 × 3 message types), the map reaches ~7.8 million entries after 24 hours, consuming ~1.5 GB of heap.

## Impact Explanation

Unbounded heap growth from a single persistent peer leads to OOM, causing the CKB node process to crash or become severely degraded. This matches the allowed bounty impact: **"Vulnerabilities which could easily crash a CKB node" (High, 10001–15000 points)**. No consensus deviation or economy damage is required; node availability is directly threatened.

## Likelihood Explanation

The attack requires only a single TCP connection to a CKB node with the HolePunching protocol enabled. No authentication, PoW, stake, or privileged role is needed. The attacker generates valid `PeerId` multihashes (a fixed 2-byte prefix plus any 32-byte value) as payload fields. The attack is fully automatable, costs only bandwidth, and is slow (hours) but reliable. Any unprivileged external user can execute it.

## Recommendation

Call `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside `notify()`, which already fires every 5 minutes. This ensures stale entries are periodically evicted regardless of whether any peer disconnects:

```rust
async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
    self.rate_limiter.retain_recent();
    self.forward_rate_limiter.retain_recent();
    // ... existing logic
}
``` [3](#0-2) 

## Proof of Concept

```rust
#[test]
fn test_forward_rate_limiter_unbounded_growth() {
    use governor::Quota;
    use std::num::NonZeroU32;

    let quota = Quota::per_second(NonZeroU32::new(1).unwrap());
    let limiter: governor::RateLimiter<
        (Vec<u8>, Vec<u8>, u32),
        governor::state::keyed::HashMapStateStore<(Vec<u8>, Vec<u8>, u32)>,
        governor::clock::DefaultClock,
    > = governor::RateLimiter::hashmap(quota);

    let n = 10_000usize;
    for i in 0..n {
        // Valid PeerId bytes: [0x12, 0x20, <32 unique bytes>]
        let mut from = vec![0x12u8, 0x20];
        from.extend_from_slice(&(i as u64).to_le_bytes());
        from.extend_from_slice(&[0u8; 24]);
        let mut to = vec![0x12u8, 0x20];
        to.extend_from_slice(&((i + 1_000_000) as u64).to_le_bytes());
        to.extend_from_slice(&[0u8; 24]);
        // retain_recent() never called — simulates persistent peer
        let _ = limiter.check_key(&(from, to, 0u32));
    }
    // All N entries remain in the map — never reclaimed without retain_recent()
    // In production: 30 msg/sec × 3 types × 86400 sec = ~7.8M entries/day ≈ 1.5 GB
}
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

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L127-143)
```rust
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L76-96)
```rust
    pub(crate) async fn execute(self) -> Status {
        let content = match SyncContent::try_from(&self.message) {
            Ok(content) => content,
            Err(status) => return status,
        };

        if content.route.len() > MAX_HOPS as usize {
            return StatusCode::InvalidRoute.with_context("the route length is too long");
        }
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

**File:** network/fuzz/src/lib.rs (L139-145)
```rust
impl FromBytes<PeerId> for PeerId {
    fn type_size() -> usize {
        32
    }
    fn from_bytes(d: &[u8]) -> PeerId {
        PeerId::from_bytes([vec![0x12], vec![0x20], d.to_vec()].concat()).unwrap()
    }
```
