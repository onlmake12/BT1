### Title
Unbounded `pending_delivered` HashMap Growth via Spoofed `from` PeerIds in `ConnectionRequest` — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

An unprivileged remote peer can exhaust heap memory on a victim CKB node by flooding it with `ConnectionRequest` P2P messages, each carrying a unique spoofed `from` PeerId and the victim's own peer ID as `to`. Because the only deduplication guard checks for a repeated `from` key, and the `forward_rate_limiter` is keyed on `(from, to, msg_item_id)`, every message with a fresh `from` bypasses all guards and inserts a new entry into `pending_delivered`. The map is only pruned every 5 minutes in `notify()`, with no size cap. A secondary unbounded structure, `forward_rate_limiter` (a `HashMapStateStore`), grows at the same rate and is only cleaned on `disconnected()`.

---

### Finding Description

**Entry point:** Any peer connected to the victim sends `ConnectionRequest` messages over the `HolePunching` P2P protocol.

**Guard 1 — session-level rate limiter** (`mod.rs` lines 95–107):
Keyed by `(session_id, msg_item_id)`. For `ConnectionRequest`, `msg_item_id = 0`, so the key is always `(session_id, 0)` — a single key per session, capped at 30 messages/second. This is the only real throttle. [1](#0-0) 

**Guard 2 — `forward_rate_limiter`** (`connection_request.rs` lines 132–143):
Keyed by `(content.from, content.to, msg_item_id)`. Quota: 1/second per key. With a unique `from` PeerId per message, every message creates a new key and passes unconditionally. This guard provides zero protection against spoofed `from` IDs. [2](#0-1) 

**Guard 3 — dedup check in `respond_delivered`** (`connection_request.rs` lines 161–167):
Only suppresses re-insertion if the same `from` PeerId was seen within `HOLE_PUNCHING_INTERVAL` (2 minutes). With unique `from` IDs, every message passes. [3](#0-2) 

**Unbounded insert** (`connection_request.rs` lines 234–237):
After `send_message_to` succeeds, a new entry `(from_peer_id → (remote_listens, now))` is unconditionally inserted. No size cap exists. [4](#0-3) 

**Cleanup only in `notify()`** (`mod.rs` lines 172–175):
`pending_delivered` is pruned by timestamp every `CHECK_INTERVAL = 5 minutes`. There is no maximum-size eviction. [5](#0-4) 

**Secondary unbounded structure — `forward_rate_limiter`** (`mod.rs` lines 45–46, 67–68):
This `HashMapStateStore`-backed rate limiter accumulates one entry per unique `(from, to, msg_item_id)` key. `retain_recent()` is only called on `disconnected()`. While the attacker maintains the connection, this map grows at the same rate as `pending_delivered` and is never pruned. [6](#0-5) [7](#0-6) 

---

### Impact Explanation

Default CKB configuration: `max_peers = 125`, `max_outbound_peers = 8`, so up to 117 inbound connections are accepted. [8](#0-7) 

**Growth rate per session:** 30 entries/second (session rate limiter cap).

**Growth rate with 117 inbound sessions:** 30 × 117 = **3,510 entries/second**.

**Entries accumulated before `notify()` cleanup (5 min):** 3,510 × 300 = **~1,053,000 entries**.

**Memory per entry:**
- Key `PeerId`: ~39 bytes
- Value `Vec<Multiaddr>`: attacker controls up to `ADDRS_COUNT_LIMIT = 24` addresses × ~30 bytes = ~720 bytes
- `u64` timestamp: 8 bytes
- HashMap overhead: ~50 bytes
- **Per entry (24 addrs): ~817 bytes**

**Total `pending_delivered` at peak:** ~1,053,000 × 817 bytes ≈ **~860 MB**

**`forward_rate_limiter` at peak:** same entry count, ~80 bytes/key + governor state ≈ **~100–200 MB additional**

**Combined peak:** ~1 GB, sufficient to OOM nodes with 1–2 GB RAM, and causes severe memory pressure on all nodes. The attack is continuous — after each 5-minute cleanup cycle, the attacker immediately refills the map.

---

### Likelihood Explanation

- Attacker only needs standard P2P connections to the victim — no authentication, no PoW, no privileged role.
- The victim's peer ID is publicly discoverable via the `get_peers` RPC or peer exchange.
- The attacker needs to supply at least one valid TCP/IP4 or TCP/IP6 address in `listen_addrs` (trivially satisfied with any routable address).
- The attack is fully automatable from a single machine with multiple connections.
- The `forward_rate_limiter` bypass (unique `from` IDs) is the intended design for routing distinct peers, not a configuration error.

---

### Recommendation

1. **Cap `pending_delivered` size**: Enforce a maximum entry count (e.g., `MAX_PENDING_DELIVERED = 1024`). When the cap is reached, reject new insertions or evict the oldest entry.
2. **Periodic `forward_rate_limiter` cleanup**: Call `self.forward_rate_limiter.retain_recent()` inside `notify()`, not only on `disconnected()`.
3. **Limit `from` PeerId insertions per session**: Track how many `pending_delivered` entries originated from each session and reject further insertions once a per-session cap is reached.
4. **Reduce `CHECK_INTERVAL`** or use a shorter `TIMEOUT` to bound the maximum map size.

---

### Proof of Concept

```rust
// Pseudocode: attacker sends from a single connection
for i in 0..9000 {
    let rand_from = PeerId::random();
    let msg = ConnectionRequest {
        from: rand_from,
        to: victim_peer_id.clone(),
        max_hops: 6,
        listen_addrs: vec!["/ip4/1.2.3.4/tcp/8115".parse().unwrap()],
        route: vec![],
    };
    send_to_victim(msg);
    // session rate limiter: 30/sec → sleep 33ms between batches
}
// After 5 minutes with 117 connections:
// pending_delivered.len() ≈ 1,053,000
// forward_rate_limiter internal map ≈ 1,053,000 entries
// Total heap: ~1 GB
assert!(victim_node.is_oom_crashed());
```

The unit test assertion `pending_delivered.len() <= CONSTANT` would fail immediately, as there is no size bound enforced between `notify()` calls.

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L45-46)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L172-175)
```rust
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** resource/ckb.toml (L94-95)
```text
max_peers = 125
max_outbound_peers = 8
```
