### Title
`forward_rate_limiter` Bypass via Rotating Spoofed `from` PeerIds in Hole-Punching `ConnectionRequest` — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

The `forward_rate_limiter` in the CKB hole-punching protocol is keyed on `(from, to, item_id)` where `from` is taken directly from the attacker-controlled message payload without being validated against the actual session's peer ID. An attacker with a single session can send 30 `ConnectionRequest` messages per second — each with a distinct random `from` PeerId — causing all 30 to pass both rate limiters and trigger `forward_message()` 30 times per second, each broadcasting to `sqrt(N)` neighbors. This also causes unbounded `forward_rate_limiter` HashMap growth during the session.

---

### Finding Description

**Two rate limiters exist:**

1. **`rate_limiter`** — keyed by `(PeerIndex, u32)` = `(session_id, msg_item_id)`, quota 30/s. [1](#0-0) [2](#0-1) 

2. **`forward_rate_limiter`** — keyed by `(PeerId, PeerId, u32)` = `(from, to, msg_item_id)`, quota 1/s. [3](#0-2) 

**Step 1 — per-session check passes:** In `received()`, the `rate_limiter` is checked against `(session_id, msg.item_id())`. With 30 messages/s from one session, all 30 pass. [4](#0-3) 

**Step 2 — `forward_rate_limiter` check passes for all 30:** In `execute()`, the check uses `content.from` which is parsed directly from the message payload — not from the session's authenticated peer ID. There is no validation that `content.from` matches the actual sender. [5](#0-4) [6](#0-5) 

Since each of the 30 messages carries a unique `from_i`, each produces a new key `(from_i, to, 0)` never seen before, so `check_key` always returns `Ok`. The 1/s-per-`(from,to)` limit is completely bypassed.

**Step 3 — `forward_message()` called 30 times/s:** When `to` is not the local node and `max_hops > 0`, `forward_message()` is invoked. If `to` is not a directly connected peer, it gossip-broadcasts to `sqrt(N)` peers. [7](#0-6) [8](#0-7) 

**Step 4 — Unbounded HashMap growth:** Each unique `(from_i, to, 0)` key is inserted into the `HashMapStateStore` backing `forward_rate_limiter`. `retain_recent()` is only called on disconnect, so during an active session the map grows at 30 entries/second without bound. [9](#0-8) 

---

### Impact Explanation

- **Forwarding amplification:** A single attacker session causes 30 × `sqrt(N)` outbound messages per second instead of the intended 1 × `sqrt(N)`. With 100 peers, that is 300 forwarded messages/s per attacker session; with 1000 peers, ~930/s.
- **Cascading amplification:** Each forwarded message carries `max_hops` up to 6 and a fresh `from` PeerId, so intermediate nodes also have their `forward_rate_limiter` bypassed by the same mechanism, multiplying the effect across the network.
- **Memory exhaustion:** The `forward_rate_limiter` HashMap grows at 30 entries/s per attacker session with no bound until disconnect.

---

### Likelihood Explanation

Any unprivileged peer that can establish a single P2P session can execute this attack. No PoW, no key material, no privileged role is required. The `from` field is a free-form payload field with no binding to the session identity. The attack is trivially scriptable.

---

### Recommendation

1. **Validate `from` against the session peer ID.** Before the `forward_rate_limiter` check, assert that `content.from` equals the peer ID associated with `self.peer` (the actual session). Reject with `InvalidFromPeerId` if they differ.
2. **Key `forward_rate_limiter` on `(session_id, to, item_id)` instead of `(from, to, item_id)`.** This makes the limit session-bound and unbypassable regardless of `from` field content.
3. **Call `retain_recent()` periodically** (e.g., in the `notify` timer) rather than only on disconnect, to bound HashMap memory growth. [10](#0-9) 

---

### Proof of Concept

```
1. Attacker connects to victim node → session S established.
2. Attacker sends 30 ConnectionRequest/s:
     for i in 0..30:
         msg.from = PeerId::random()   // unique each time
         msg.to   = fixed_target_peer_id
         msg.max_hops = 6
         msg.listen_addrs = [valid_addr]
         send(msg)

3. In received() [mod.rs:95-107]:
     rate_limiter.check_key(&(S, ConnectionRequest_item_id))
     → bucket has capacity 30 → all 30 pass ✓

4. In execute() [connection_request.rs:132-143]:
     forward_rate_limiter.check_key(&(from_i, fixed_to, 0))
     → key (from_i, fixed_to, 0) is new for every i → all 30 pass ✓

5. forward_message() called 30×/s:
     filter_broadcast to sqrt(N) peers each time
     → 30 × sqrt(N) outbound messages/s from victim

6. forward_rate_limiter HashMap: 30 new entries inserted per second,
   never pruned until disconnect → memory grows without bound.

Differential assertion:
   fixed_from_rate   = measure forwarding rate with same from each msg  → ~1/s (rate limited)
   rotating_from_rate = measure forwarding rate with unique from each msg → ~30/s (bypassed)
   assert_eq!(fixed_from_rate, rotating_from_rate)  // FAILS
```

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

**File:** network/src/protocols/hole_punching/mod.rs (L169-175)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
        let status = self.network_state.connection_status();

        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/mod.rs (L249-252)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (CHECK_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-152)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
        } else if content.max_hops == 0u8 {
            StatusCode::ReachedMaxHops.into()
        } else {
            self.forward_message(self_peer_id, &content.to).await
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L279-305)
```rust
                // Broadcast to a number of nodes equal to the square root of the total connection count using gossip.
                let sid = self.peer;
                let mut total = self
                    .protocol
                    .network_state
                    .with_peer_registry(|p| p.peers().len())
                    .isqrt();
                if let Err(error) = self
                    .p2p_control
                    .filter_broadcast(
                        TargetSession::Filter(Box::new(move |id| {
                            if id == &sid {
                                return false;
                            }
                            total = total.saturating_sub(1);
                            total != 0
                        })),
                        proto_id,
                        new_message,
                    )
                    .await
                {
                    StatusCode::BroadcastError.with_context(error)
                } else {
                    Status::ok()
                }
            }
```
