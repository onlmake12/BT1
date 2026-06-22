### Title
Hole-Punching `forward_rate_limiter` Bypassed via Attacker-Controlled `from`/`to` Fields — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

The `HolePunching` protocol's `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)`, where `content.from` and `content.to` are taken directly from the attacker-supplied message body and are never verified against the actual sending session. Any connected peer can trivially bypass the forwarding rate limit by cycling through arbitrary fake `from`/`to` peer ID pairs, causing the node to forward an unbounded number of hole-punching messages per second (up to the outer per-session cap of 30 req/s, all of which pass the inner limiter).

---

### Finding Description

**Root cause — unverified key fields in the forwarding rate limiter**

In `network/src/protocols/hole_punching/mod.rs`, two rate limiters are created:

```rust
// outer: keyed by (session_id, msg_type) — correctly uses the real session
let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
let rate_limiter = RateLimiter::hashmap(quota);

// inner: keyed by (from, to, msg_type) — from/to come from the message body
let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
let forward_rate_limiter = RateLimiter::hashmap(quota);
``` [1](#0-0) 

The outer limiter is checked first, keyed by the real `session_id` (not attacker-controlled):

```rust
if self.rate_limiter.check_key(&(session_id, msg.item_id())).is_err() { … }
``` [2](#0-1) 

Then, inside `ConnectionRequestProcess::execute()`, the inner `forward_rate_limiter` is checked:

```rust
if self.protocol.forward_rate_limiter
    .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
    .is_err()
{ … return StatusCode::TooManyRequests … }
``` [3](#0-2) 

`content.from` and `content.to` are deserialized from the message payload. There is **no check** that `content.from` matches the actual sending session's peer ID. The same pattern appears in `ConnectionRequestDeliveredProcess` and `ConnectionSyncProcess`: [4](#0-3) [5](#0-4) 

**Exploit path**

1. Attacker connects to a CKB node as a normal P2P peer (no privilege required).
2. Attacker sends up to 30 `ConnectionRequest` messages per second (the outer limiter allows this).
3. Each message carries a distinct, fabricated `(from, to)` peer ID pair.
4. Each unique pair creates a fresh bucket in the `forward_rate_limiter` hashmap, so all 30 messages pass the inner check.
5. The node forwards all 30 messages to other connected peers — the inner limiter provides zero additional protection.
6. The attacker can sustain this indefinitely, growing the `forward_rate_limiter` hashmap without bound (one entry per unique `(from, to)` pair used).

The comment in the code states the intent: *"the same group of from/to should not be received by the same node more than 1 times within one second."* That invariant is completely defeated. [6](#0-5) 

---

### Impact Explanation

- **Forwarding amplification**: The node forwards all attacker-crafted messages to real peers, wasting outbound bandwidth and CPU on message serialization/sending.
- **Unbounded hashmap growth**: The `forward_rate_limiter` is a `HashMapStateStore` keyed by `(PeerId, PeerId, u32)`. Each unique `(from, to)` pair the attacker uses inserts a new entry. Over time this grows the node's memory without bound. (`retain_recent()` is only called on disconnect, not periodically during the session.)
- **Routing-loop risk**: An attacker can craft `from`/`to` pairs that cause the node to forward messages back toward peers that will re-forward them, creating amplification loops across the gossip graph.
- **Denial of service**: Sustained at 30 req/s per connection, and with multiple attacker connections, the node's forwarding capacity and memory are exhausted. [7](#0-6) 

---

### Likelihood Explanation

- **Attacker preconditions**: Only a standard P2P connection is required — no keys, no stake, no privileged role.
- **Ease of exploit**: Sending crafted molecule-encoded messages with varying `from`/`to` byte sequences is trivial with any P2P client.
- **No detection**: The node logs only at `debug` level for rate-limit hits; since the inner limiter is never triggered, no warning is emitted.

---

### Recommendation

1. **Verify `content.from` against the actual session**: After parsing, assert that `content.from` equals the peer ID of the sending session. Reject (and ban) messages where `content.from` does not match.
2. **Re-key the `forward_rate_limiter` by session**: Use `(peer_index, msg_item_id)` — the same key as the outer limiter — so the forwarding budget is tied to the real sender, not to attacker-supplied fields.
3. **Periodically prune the hashmap**: Call `forward_rate_limiter.retain_recent()` on a timer (e.g., every `CHECK_INTERVAL`) rather than only on disconnect, to bound memory growth.

---

### Proof of Concept

```
1. Connect to a CKB node on the HolePunching protocol (SupportProtocols::HolePunching).
2. In a loop (≤30 iterations/second to stay under the outer limiter):
   a. Generate a fresh random PeerId for `from` (or `to`).
   b. Build a valid ConnectionRequest molecule message with that `from`, any `to`,
      max_hops=6, listen_addrs=[any valid multiaddr], route=[].
   c. Send it to the node.
3. Observe: the node forwards each message to other peers (no TooManyRequests returned).
4. After N seconds, the node's forward_rate_limiter hashmap contains N*30 entries,
   and N*30 forwarding sends have been issued to other peers.
```

The outer `rate_limiter` (30 req/s per session) is the only real bound; the `forward_rate_limiter` is fully neutralized by the attacker's ability to supply arbitrary `from`/`to` values.

### Citations

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

**File:** network/src/protocols/hole_punching/mod.rs (L249-257)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (CHECK_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L85-95)
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
```
