### Title
Forward Rate Limiter Bypass via Attacker-Controlled `content.from`/`content.to` in Hole Punching Protocol - (File: `network/src/protocols/hole_punching/component/connection_request.rs`, `connection_request_delivered.rs`, `connection_sync.rs`)

---

### Summary

The CKB hole punching protocol uses two rate limiters. The outer `rate_limiter` is correctly keyed by the actual network `session_id` (unforgeable). The inner `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)` — values read directly from the attacker-controlled message payload. An unprivileged connected peer can bypass the forwarding rate limit entirely by rotating `content.from` or `content.to` in successive messages, causing unlimited message forwarding and unbounded memory growth in the limiter's internal state on every intermediate node.

---

### Finding Description

`HolePunching` maintains two rate limiters:

```rust
rate_limiter: RateLimiter<(PeerIndex, u32)>,
forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
``` [1](#0-0) 

The outer limiter is applied first in `received()`, keyed by the actual `session_id` — a value assigned by the network layer that the peer cannot forge:

```rust
if self.rate_limiter.check_key(&(session_id, msg.item_id())).is_err() { ... }
``` [2](#0-1) 

This correctly limits any single peer to 30 messages/sec regardless of message content.

The inner `forward_rate_limiter` is then checked inside each message processor. In `ConnectionRequestProcess::execute()`:

```rust
if self.protocol.forward_rate_limiter
    .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
    .is_err()
{ ... }
``` [3](#0-2) 

`content.from` and `content.to` are parsed directly from the message bytes sent by the peer:

```rust
let from = PeerId::from_bytes(value.from().raw_data().to_vec())...;
let to   = PeerId::from_bytes(value.to().raw_data().to_vec())...;
``` [4](#0-3) 

The same pattern is repeated identically in `ConnectionRequestDeliveredProcess::execute()` and `ConnectionSyncProcess::execute()`: [5](#0-4) [6](#0-5) 

Because the `forward_rate_limiter` key is entirely derived from the message payload, an attacker can generate a fresh, never-seen `(from, to)` pair for every message. Each new pair gets its own independent rate-limit bucket, so the 1-req/sec-per-pair cap is never triggered. The attacker is still bounded by the outer 30 req/sec cap per session, but the `forward_rate_limiter` — whose purpose is to prevent the same forwarding path from being amplified — provides zero protection.

A secondary consequence: the `governor::RateLimiter<_, HashMapStateStore<_>, _>` allocates a new map entry for every unique key. With 30 unique `(from, to)` pairs per second per session, the limiter's internal `HashMapStateStore` grows without bound. `retain_recent()` is called only on disconnect:

```rust
async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
    self.rate_limiter.retain_recent();
    self.forward_rate_limiter.retain_recent();
``` [7](#0-6) 

A long-lived connection that never disconnects will accumulate entries indefinitely.

Additionally, in `respond_delivered()`, the `pending_delivered` map is keyed by `from_peer_id` (= `content.from`), so an attacker targeting a specific node can insert unlimited entries into that map as well:

```rust
self.protocol.pending_delivered.insert(from_peer_id, (remote_listens, now));
``` [8](#0-7) 

---

### Impact Explanation

**Impact: Medium.**

- The `forward_rate_limiter` invariant — "the same `(from, to)` forwarding pair must not be processed more than once per second" — is completely broken. An attacker can drive any intermediate node to forward up to 30 hole-punching messages per second (the outer cap), each with a distinct spoofed `from`/`to`, causing amplified traffic across the P2P network.
- The `forward_rate_limiter`'s `HashMapStateStore` and the `pending_delivered` map grow without bound for the duration of a connection, consuming memory proportional to the number of messages sent.
- No consensus state is corrupted, but network resource exhaustion on intermediate nodes is achievable by any connected peer.

---

### Likelihood Explanation

**Likelihood: High.**

Any peer that has established a single HolePunching protocol session can exploit this. No special privileges, keys, or majority hashpower are required. The attacker simply sends valid `ConnectionRequest` messages with a different random `from` PeerId in each one. The outer 30 req/sec cap is the only real constraint, making this trivially and continuously exploitable.

---

### Recommendation

Key the `forward_rate_limiter` by the actual network peer identity (`session_id` / `PeerIndex`) rather than by attacker-controlled message fields. The outer `rate_limiter` already demonstrates the correct pattern:

```rust
// Correct: keyed by unforgeable session identity
self.rate_limiter.check_key(&(session_id, msg.item_id()))

// Fix: replace forward_rate_limiter key with peer + item_id
self.forward_rate_limiter.check_key(&(self.peer, self.msg_item_id))
```

If the semantic intent is to deduplicate forwarding of the same logical `(from, to)` request across *different* peers, a separate bounded cache (e.g., an LRU set of recently seen `(from, to, item_id)` tuples with a fixed capacity) should be used instead of an unbounded hash-map-backed rate limiter keyed on attacker-supplied data.

---

### Proof of Concept

1. Attacker connects to a CKB node and negotiates the `HolePunching` protocol.
2. Attacker sends up to 30 `ConnectionRequest` messages per second (outer cap), each with a freshly generated random `from` PeerId (e.g., `PeerId::random()`), a fixed `to` PeerId, and a valid `listen_addrs` list.
3. For each message, `forward_rate_limiter.check_key(&(content.from, content.to, item_id))` sees a brand-new key and passes immediately — the 1-req/sec limit is never triggered.
4. The node forwards all 30 messages per second to its peers, who in turn forward them further, amplifying traffic across the network.
5. After N seconds, the `forward_rate_limiter`'s internal `HashMapStateStore` contains N×30 entries, growing without bound until the attacker disconnects.

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-40)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
            .map_err(|_| StatusCode::InvalidToPeerId.with_context("the to peer id is invalid"))?;
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L235-237)
```rust
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
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
