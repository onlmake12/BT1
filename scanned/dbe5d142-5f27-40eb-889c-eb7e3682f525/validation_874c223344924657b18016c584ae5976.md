### Title
HolePunching Per-Peer Rate Limit Bypassed by Disconnect-Reconnect (Session ID Reset) — (`File: network/src/protocols/hole_punching/mod.rs`)

---

### Summary

The `HolePunching` protocol enforces a per-peer rate limit of 30 messages/second using a `RateLimiter` keyed by `(PeerIndex, msg_item_id)`. Because `PeerIndex` is a type alias for `SessionId` — a session-scoped integer that is freshly assigned on every new TCP connection — an attacker can reset their rate-limit bucket to zero simply by disconnecting and reconnecting. This is the direct CKB analog of the HoldefiSettings "remove-and-re-add" bypass: the restriction state is tied to a transient identifier, so destroying and recreating the entity resets the restriction.

---

### Finding Description

`PeerIndex` is defined as a pure alias for `SessionId`:

```rust
// network/src/protocols/mod.rs, line 33
pub type PeerIndex = SessionId;
```

The `HolePunching` struct holds two rate limiters:

```rust
// network/src/protocols/hole_punching/mod.rs, lines 45-46
rate_limiter: RateLimiter<(PeerIndex, u32)>,
forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```

Every incoming HolePunching message is checked against `rate_limiter` before processing:

```rust
// network/src/protocols/hole_punching/mod.rs, lines 95-107
if self
    .rate_limiter
    .check_key(&(session_id, msg.item_id()))
    .is_err()
{
    ...
    return;
}
```

The quota is 30 requests/second per `(PeerIndex, msg_item_id)` pair:

```rust
// network/src/protocols/hole_punching/mod.rs, lines 251-252
let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
let rate_limiter = RateLimiter::hashmap(quota);
```

On disconnect, `retain_recent()` is called — this only evicts *stale* entries from the hashmap, it does **not** remove the specific disconnecting peer's entry:

```rust
// network/src/protocols/hole_punching/mod.rs, lines 66-70
async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
    self.rate_limiter.retain_recent();
    self.forward_rate_limiter.retain_recent();
    ...
}
```

When the same physical peer reconnects, the p2p layer assigns a new `SessionId` (e.g., old session was `5`, new session is `6`). The `rate_limiter` has no entry for `(6, item_id)`, so the bucket starts completely fresh. The old entry for `(5, item_id)` sits in the hashmap until the next `retain_recent()` sweep cleans it up.

The `forward_rate_limiter` is keyed by `(PeerId, PeerId, u32)` — identity-based and therefore **not** bypassed by reconnection. Only the primary `rate_limiter` is vulnerable.

**Bypass sequence:**
1. Attacker connects → gets `SessionId = N`.
2. Attacker sends 30 HolePunching messages → bucket for `(N, item_id)` is exhausted.
3. Attacker disconnects → `retain_recent()` runs but does not remove the exhausted entry immediately.
4. Attacker reconnects → gets `SessionId = N+1`.
5. Bucket for `(N+1, item_id)` is empty → attacker can send 30 more messages immediately.
6. Repeat indefinitely.

---

### Impact Explanation

An unprivileged remote peer can flood a CKB node with HolePunching messages (`ConnectionRequest`, `ConnectionRequestDelivered`, `ConnectionSync`) at rates far exceeding the intended 30/second cap. Each `ConnectionSync` message can trigger forwarding to other connected peers (up to `sqrt(total)` peers via gossip broadcast), creating a **message amplification** effect across the network. Sustained flooding can exhaust CPU and network bandwidth on the victim node and its neighbors, degrading or halting normal block/transaction relay.

---

### Likelihood Explanation

Any peer that can establish a TCP connection to a CKB node can exploit this. No special privileges, keys, or majority hashpower are required. The only cost is the TCP handshake overhead per reconnect cycle, which is negligible compared to the amplification gain. The HolePunching protocol is enabled by default and reachable from the public internet.

---

### Recommendation

Key the primary rate limiter by `PeerId` (stable cryptographic identity) rather than `PeerIndex` (ephemeral session ID), matching the approach already used by `forward_rate_limiter`:

```rust
// Change:
rate_limiter: RateLimiter<(PeerIndex, u32)>,
// To:
rate_limiter: RateLimiter<(PeerId, u32)>,
```

In the `received` handler, extract the `PeerId` from the session context (available via `context.session.address`) and use it as the rate-limit key instead of `session_id`. This ensures that reconnecting does not reset the rate-limit bucket, because the bucket is now tied to the peer's cryptographic identity rather than its transient session handle.

---

### Proof of Concept

```
1. Attacker opens connection to victim CKB node.
   → Assigned SessionId = 100 (PeerIndex = 100).

2. Attacker sends 30 ConnectionRequest messages in <1 second.
   → rate_limiter[(100, item_id)] bucket exhausted.
   → 31st message is silently dropped.

3. Attacker closes TCP connection.
   → disconnected() fires; retain_recent() runs but entry (100, item_id)
     is still "recent" and remains in the hashmap.

4. Attacker immediately reconnects.
   → Assigned SessionId = 101 (PeerIndex = 101).
   → rate_limiter has NO entry for (101, item_id).

5. Attacker sends 30 more ConnectionRequest messages in <1 second.
   → All 30 pass the rate check (fresh bucket).
   → Each triggers a gossip broadcast to sqrt(N) peers.

6. Repeat from step 3 at will.
   → Effective throughput: 30 * reconnect_rate messages/second,
     unbounded by the intended 30/second cap.
```

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** network/src/protocols/mod.rs (L33-33)
```rust
pub type PeerIndex = SessionId;
```

**File:** network/src/protocols/hole_punching/mod.rs (L45-46)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L249-252)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (CHECK_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```
