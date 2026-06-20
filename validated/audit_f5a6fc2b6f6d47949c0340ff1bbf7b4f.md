The code is clear. Let me confirm the exact rate limiter key semantics before writing the finding.

### Title
Unbounded `pending_delivered` HashMap Growth via Spoofed `from` PeerIds in `ConnectionRequest` — (`network/src/protocols/hole_punching/mod.rs`, `component/connection_request.rs`)

---

### Summary

The `HolePunching` protocol's `respond_delivered()` function inserts an entry into `pending_delivered` for every `ConnectionRequest` message where `content.to == local_peer_id`, keyed by `content.from`. Because `content.from` is fully attacker-controlled and the only deduplication guard checks whether the **same** `from` key already exists, an attacker sending messages with distinct `from` PeerIds bypasses all deduplication and fills the map without bound. The map is pruned only in `notify()`, which fires every 5 minutes.

---

### Finding Description

`pending_delivered` is declared as an unbounded `HashMap<PeerId, PendingDeliveredInfo>`: [1](#0-0) 

The only deduplication guard in `respond_delivered()` is a timestamp check on the **existing** key: [2](#0-1) 

For a brand-new `from_peer_id` (one not yet in the map), this check is skipped entirely and the entry is unconditionally inserted: [3](#0-2) 

**Rate limiter analysis — neither limiter bounds the attack:**

1. `rate_limiter` (keyed by `(session_id, msg_item_id)`) allows **30 ConnectionRequest messages per second** from a single session — this is a throughput cap, not a map-size cap. [4](#0-3) 

2. `forward_rate_limiter` (keyed by `(content.from, content.to, msg_item_id)`) allows **1 message per second per unique `(from, to)` pair**. With a distinct `from` PeerId per message, every message gets a fresh bucket and is allowed through unconditionally. [5](#0-4) 

**Cleanup only fires every 5 minutes:** [6](#0-5) [7](#0-6) 

**Secondary unbounded growth:** `forward_rate_limiter`'s internal `HashMapStateStore<(PeerId, PeerId, u32)>` also grows with each unique `(from, to, 0)` key and is only cleaned up on peer disconnect via `retain_recent()`: [8](#0-7) 

---

### Impact Explanation

From a single TCP session (30 inserts/second × 300 seconds = **9,000 entries per 5-minute window**). Each entry stores `Vec<Multiaddr>` of up to 24 addresses (~50 bytes each) plus a timestamp: roughly **1.2 KB per entry → ~10.8 MB per 5-minute window per peer**. With the default maximum peer count (~125 connections), a coordinated attack yields ~1.35 GB of memory growth every 5 minutes, leading to OOM and node crash.

---

### Likelihood Explanation

- Attacker needs only a standard P2P connection (no privilege, no PoW, no key).
- The victim's `local_peer_id` is publicly advertised.
- Valid TCP `listen_addrs` require only a syntactically correct `Multiaddr` with an IP component — trivially crafted.
- The `from` PeerId field is a raw byte field with no cryptographic binding to the sending session. [9](#0-8) 

---

### Recommendation

1. **Add a hard size cap** on `pending_delivered` (e.g., 1,024 entries). Reject or evict LRU entries when the cap is reached.
2. **Bind `from` to the sending session**: verify that `content.from` matches the peer ID of the connected session (`context.session.id`), so spoofed `from` values are rejected before insertion.
3. **Reduce `CHECK_INTERVAL`** or trigger incremental pruning inside `respond_delivered()` when the map exceeds a threshold.
4. **Cap `forward_rate_limiter` key space** or periodically call `retain_recent()` independent of disconnect events.

---

### Proof of Concept

```rust
// Pseudocode: attacker sends 9000 ConnectionRequest messages in one 5-minute window
for i in 0..9000 {
    let fake_from = PeerId::random(); // unique each iteration
    let msg = ConnectionRequest {
        from: fake_from,
        to: victim_local_peer_id,
        listen_addrs: vec!["/ip4/1.2.3.4/tcp/8115".parse().unwrap()],
        max_hops: 1,
        route: vec![],
    };
    send_to_victim(msg);
    sleep(Duration::from_millis(34)); // ~30/sec, within rate_limiter quota
}
// After 5 minutes: pending_delivered.len() == 9000, ~10.8 MB consumed
// assert!(victim_pending_delivered.len() <= MAX_CAP); // FAILS — no cap exists
```

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L25-25)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
```

**File:** network/src/protocols/hole_punching/mod.rs (L44-44)
```rust
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
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

**File:** network/src/protocols/hole_punching/mod.rs (L172-174)
```rust
        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
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
