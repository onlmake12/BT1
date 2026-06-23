### Title
Unbounded `pending_delivered` HashMap Growth via Unique-`from` ConnectionRequest Flood — (`network/src/protocols/hole_punching/mod.rs`, `component/connection_request.rs`)

---

### Summary

An unprivileged remote peer can exhaust heap memory on a victim CKB node by sending `ConnectionRequest` messages at the per-session rate limit (30/sec) with a unique, attacker-controlled `from` PeerId per message and `to` set to the victim's own PeerId. The intended `forward_rate_limiter` guard is keyed on the attacker-controlled `(from, to, msg_item_id)` tuple and is trivially bypassed. The `pending_delivered` map and the rate limiter's own internal state both grow without bound between cleanup ticks.

---

### Finding Description

**Two rate limiters exist, but only one is effective:**

The outer `rate_limiter` in `received()` is keyed by `(session_id, msg.item_id())` — i.e., `(PeerIndex, u32)`. This correctly limits a single session to 30 `ConnectionRequest` messages per second. [1](#0-0) 

The inner `forward_rate_limiter` in `execute()` is keyed by `(content.from.clone(), content.to.clone(), self.msg_item_id)`. It is intended to deduplicate forwarded requests, but `content.from` is fully attacker-controlled wire data. With a unique `from` PeerId per message, every message produces a distinct key and passes the limiter unconditionally. [2](#0-1) 

**The `respond_delivered` dedup check is also bypassed:**

The only other guard inside `respond_delivered` checks whether `pending_delivered` already contains the `from_peer_id`. With a unique `from` per message, no existing entry is ever found, so the check always passes. [3](#0-2) 

**Unbounded insertion into `pending_delivered`:**

After passing all guards, `respond_delivered` inserts `(remote_listens, now)` into `pending_delivered` keyed by `from_peer_id`. There is no cap on the map's size. [4](#0-3) 

`PendingDeliveredInfo` is `(Vec<Multiaddr>, u64)`, and each entry can hold up to `ADDRS_COUNT_LIMIT = 24` `Multiaddr` objects. [5](#0-4) 

**Cleanup only runs every 5 minutes:**

`notify()` retains only entries younger than `TIMEOUT = 5 minutes`. Between ticks, all inserted entries accumulate. [6](#0-5) 

**Secondary unbounded growth — the rate limiter's own internal state:**

`forward_rate_limiter` uses `governor::state::keyed::HashMapStateStore<(PeerId, PeerId, u32)>`. Each unique `(from, to, msg_item_id)` tuple creates a new entry in this store. It is only cleaned via `retain_recent()` on `disconnected()` — never during a live session. A long-lived attacker session therefore causes the rate limiter's internal map to grow continuously alongside `pending_delivered`. [7](#0-6) [8](#0-7) 

---

### Impact Explanation

In a single 5-minute window, one attacker session inserts up to `30 × 300 = 9,000` entries into `pending_delivered`. Each entry holds up to 24 `Multiaddr` objects (~800–1,250 bytes per entry), yielding ~11 MB per session per window. The `forward_rate_limiter` internal store accumulates the same 9,000 keys and is never pruned during the session. With multiple concurrent attacker sessions (the P2P layer does not restrict the number of connections to a single IP by default), memory consumption scales linearly. Sustained over hours, this leads to OOM crash, node unavailability, and potential consensus deviation. [9](#0-8) 

---

### Likelihood Explanation

The attack requires only a single valid P2P connection — no PoW, no trusted role, no key material. The `from` field is raw wire bytes parsed into a `PeerId` with no binding to the actual session identity. Generating unique valid `PeerId` bytes (multihash-encoded public keys or random 32-byte identities accepted by `PeerId::from_bytes`) is trivial. The attacker must include at least one valid TCP multiaddr (e.g., `/ip4/1.2.3.4/tcp/1234`) to pass the `remote_listens.is_empty()` guard, which is also trivial. [10](#0-9) 

---

### Recommendation

1. **Cap `pending_delivered` size**: Enforce a hard upper bound (e.g., 1,024 entries). Reject or evict LRU entries when the cap is reached.
2. **Key the `forward_rate_limiter` on session identity, not wire `from`**: Use `(session_id, to, msg_item_id)` or `(session_id, msg_item_id)` so the rate limit cannot be bypassed by rotating the attacker-controlled `from` field.
3. **Periodic pruning of the rate limiter**: Call `forward_rate_limiter.retain_recent()` inside `notify()`, not only on `disconnected()`, to prevent unbounded growth of the limiter's internal state during long-lived sessions. [6](#0-5) 

---

### Proof of Concept

```rust
// Pseudocode unit test
let mut protocol = HolePunching::new(network_state);
let session_id = PeerIndex::new(1);

for i in 0..9000 {
    let unique_from = PeerId::random(); // unique per iteration
    let msg = build_connection_request(
        from: unique_from,
        to: victim_peer_id,          // self
        listen_addrs: vec!["/ip4/1.2.3.4/tcp/1234".parse().unwrap()],
        max_hops: 1,
        route: vec![],
    );
    // rate_limiter allows 30/sec; simulate at that rate
    protocol.received(session_id, msg).await;
}

// No notify() tick has fired yet
assert!(protocol.pending_delivered.len() <= BOUND); // FAILS: len == 9000
```

Each iteration uses a fresh `from` PeerId, bypassing both the `forward_rate_limiter` (new key each time) and the `respond_delivered` dedup check (no existing entry). After 9,000 iterations without a `notify()` tick, `pending_delivered.len() == 9,000` with no bound enforced. [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L27-30)
```rust
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes

type PendingDeliveredInfo = (Vec<Multiaddr>, u64);
```

**File:** network/src/protocols/hole_punching/mod.rs (L44-46)
```rust
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L161-167)
```rust
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
            }
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L196-219)
```rust
        let remote_listens: Vec<Multiaddr> = remote_listens
            .into_iter()
            .filter_map(|addr| match find_type(&addr) {
                TransportType::Memory
                | TransportType::Onion
                | TransportType::Ws
                | TransportType::Wss
                | TransportType::Tls => None,
                TransportType::Tcp => {
                    if addr
                        .iter()
                        .any(|p| matches!(p, Protocol::Ip4(_) | Protocol::Ip6(_)))
                    {
                        Some(addr)
                    } else {
                        None
                    }
                }
            })
            .collect();

        if remote_listens.is_empty() {
            return StatusCode::Ignore.with_context("remote listen address is empty");
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```
