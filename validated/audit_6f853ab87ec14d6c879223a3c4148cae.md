### Title
Unbounded `pending_delivered` HashMap Growth via Distinct `from` PeerIds in `ConnectionRequest` — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

`HolePunching::respond_delivered` inserts one entry per unique `content.from` PeerId into `pending_delivered` with no cap on the map's size. The only deduplication guard checks whether the *same* `from_peer_id` was seen recently; it is trivially bypassed by rotating distinct `from` PeerIds. The sole rate limit is 30 req/sec per `(session_id, msg_item_id)` pair, which still allows thousands of entries per cleanup window. Cleanup only occurs in `notify()` every 5 minutes. A second unbounded structure, `forward_rate_limiter`'s internal `HashMapStateStore`, also grows with each unique `(from, to, item_id)` triple and is only pruned on peer disconnect.

---

### Finding Description

**Entry point** — `HolePunching::received()` in `mod.rs`, reachable by any connected P2P peer with no authentication.

**Session-level rate limiter** is keyed by `(session_id, msg.item_id())`: [1](#0-0) 

This allows **30 `ConnectionRequest` messages per second per session**. It does not key on `content.from`, so rotating `from` PeerIds does not trigger it faster.

**Forward rate limiter** is keyed by `(content.from, content.to, msg_item_id)`: [2](#0-1) 

With a distinct `from` PeerId per message, every message is a **new key** — the forward rate limiter never fires. Its internal `HashMapStateStore` also accumulates one entry per unique triple and is only pruned via `retain_recent()` on disconnect, not in `notify()`. [3](#0-2) 

**Deduplication guard in `respond_delivered`** only prevents re-insertion for the *same* `from_peer_id`: [4](#0-3) 

A new `from` PeerId skips this check entirely and proceeds to unconditional insertion: [5](#0-4) 

**`pending_delivered` has no size cap** — it is a plain `HashMap<PeerId, (Vec<Multiaddr>, u64)>`: [6](#0-5) 

**Cleanup** is time-based only, firing every `CHECK_INTERVAL = 5 minutes`, removing entries older than `TIMEOUT = 5 minutes`: [7](#0-6) 

---

### Impact Explanation

**Single session:** 30 entries/sec × 300 sec = **9,000 entries** per 5-minute window. Each entry holds up to 24 `Multiaddr` objects (~50 bytes each) plus a `PeerId` (~39 bytes) and a `u64`. Rough per-entry cost: ~1.3 KB. Total: ~**12 MB per window** from one session — not immediately OOM but a measurable, sustained leak.

**Multiple sessions (realistic):** A CKB node accepts many inbound connections. With N sessions, growth scales linearly: N × 9,000 entries per window. At 100 sessions: ~900,000 entries ≈ **~1.2 GB** within a single 5-minute window — OOM-grade on typical node hardware.

The `forward_rate_limiter` `HashMapStateStore` adds a parallel unbounded structure growing at the same rate, compounding heap pressure.

---

### Likelihood Explanation

The attack requires only a standard P2P connection — no privileged role, no PoW, no key material. The attacker generates arbitrary valid `PeerId` bytes (any valid multihash) and valid TCP `Multiaddr` values (e.g., `0.0.0.0:1234`). The session rate limiter (30/sec) is the only real throttle and is not a meaningful defense at scale. The attack is locally reproducible without mainnet access.

---

### Recommendation

1. **Cap `pending_delivered`**: enforce a maximum size (e.g., 1,024 entries) and reject or evict on overflow.
2. **Cap `inflight_requests`** similarly.
3. **Prune `forward_rate_limiter` in `notify()`**, not only on disconnect — call `self.forward_rate_limiter.retain_recent()` alongside the existing `pending_delivered` cleanup.
4. Consider keying the session rate limiter on `(session_id, msg_item_id)` **and** `content.from` to prevent per-session key rotation.

---

### Proof of Concept

```
1. Connect to target node (establish one P2P session).
2. For i in 0..10_000:
     from_i = generate_valid_peer_id(i)   // distinct multihash bytes each iteration
     send ConnectionRequest {
         from: from_i,
         to:   local_peer_id,             // target's own PeerId
         max_hops: 1,
         route: [],
         listen_addrs: ["/ip4/1.2.3.4/tcp/4321"]  // one valid TCP addr
     }
     sleep(1/30 sec)                      // stay within session rate limit
3. Before next notify() fires (~5 min):
     assert pending_delivered.len() == 10_000
     measure heap delta (expected: ~13 MB from this session alone)
4. Open 100 parallel sessions and repeat → ~1.2 GB heap growth within one window.
```

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L44-44)
```rust
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
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
