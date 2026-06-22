Looking at the exact code path in `execute()` and the `inflight_requests` management in `notify()`.

### Title
Unauthenticated `from` Field in `ConnectionRequestDelivered` Allows Inflight Request Eviction — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

---

### Summary

The `ConnectionRequestDeliveredProcess::execute()` function determines whether the local node is the intended recipient of a delivered hole-punch response by comparing `self_peer_id` against `content.from` — a field taken verbatim from the incoming P2P message with no binding to the actual sender's authenticated session identity. Any peer with an open session to the victim can craft a `ConnectionRequestDelivered` message with `from` set to the victim's own `PeerId`, causing the victim to unconditionally evict a live `inflight_requests` entry and then silently ignore the legitimate response when it arrives.

---

### Finding Description

**Entry point — `notify()` inserts the inflight entry:**

In `mod.rs`, the `notify()` timer broadcasts `ConnectionRequest` messages and records each target:

```rust
// mod.rs line 239-242
let now = unix_time_as_millis();
for peer_id in inflight {
    self.inflight_requests.insert(peer_id, now);
}
``` [1](#0-0) 

The broadcast uses gossip (`filter_broadcast` to `sqrt(total)` peers), so any connected peer can observe the `from` and `to` fields of the `ConnectionRequest`. [2](#0-1) 

**The unauthenticated branch in `execute()`:**

When a `ConnectionRequestDelivered` arrives with an empty `route`, the code checks:

```rust
// connection_request_delivered.rs line 150-160
let self_peer_id = self.protocol.network_state.local_peer_id();
if self_peer_id != &content.from {
    self.forward_delivered(&content.from).await
} else {
    // ...
    let request_start = self.protocol.inflight_requests.remove(&content.to);
``` [3](#0-2) 

`content.from` is parsed directly from the message bytes:

```rust
// connection_request_delivered.rs line 38-40
let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
    StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
})?;
``` [4](#0-3) 

There is **no check** that `content.from` equals the peer ID of the actual session (`self.peer`). The `self.peer` field (the real sender's `PeerIndex`) is only used later in `respond_sync` to send a reply back to the attacker. [5](#0-4) 

**What happens after eviction:**

Once `inflight_requests.remove(&content.to)` returns `Some(start)` (the entry existed), the code proceeds to `respond_sync` (sending a `ConnectionSync` to the attacker's session) and `try_nat_traversal` (attempting TCP connections to the attacker's supplied addresses for up to 30 seconds). [6](#0-5) 

When the **legitimate** `ConnectionRequestDelivered` from the real target peer arrives afterward, `inflight_requests.remove(&content.to)` returns `None`, and the code returns `StatusCode::Ignore`, permanently aborting the NAT traversal:

```rust
None => StatusCode::Ignore.with_context("the request is not in flight"),
``` [7](#0-6) 

**Rate limiters do not prevent this:**

- The outer per-session rate limiter (30 req/s per `(session_id, item_id)`) allows the single attack message through. [8](#0-7) 
- The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)`. Since the attacker controls `content.from` and `content.to`, they can match the exact key of the legitimate request and still pass (1 per second is sufficient for a single-shot attack). [9](#0-8) 

---

### Impact Explanation

- The victim's `inflight_requests` entry for a specific target peer is silently deleted.
- The legitimate `ConnectionRequestDelivered` from the real target is ignored, permanently aborting the hole-punch for that peer.
- The victim wastes up to 30 seconds of TCP connection attempts toward attacker-controlled addresses.
- The victim leaks a `ConnectionSync` message to the attacker, confirming the timing of the original request.
- No ban or error is triggered on the victim side; `StatusCode::Ignore` (5xx) only produces a `warn!` log. [10](#0-9) 

---

### Likelihood Explanation

Preconditions are low-friction:
1. The attacker needs one direct P2P session to the victim — a normal peer connection.
2. The target `PeerId` is observable from the gossip broadcast of `ConnectionRequest` (sent to `sqrt(total)` peers).
3. The attack message is a single well-formed `ConnectionRequestDelivered` with `from=victim_peer_id`, `to=target_peer_id`, empty `route`, and one valid TCP `listen_addr`.
4. No cryptographic material, no PoW, no privileged role required.

---

### Recommendation

Bind the `from` field to the authenticated session identity. In `execute()`, after parsing `content.from`, verify it matches the peer ID of the actual sender:

```rust
// After parsing content, before the route check:
let sender_peer_id = self.protocol.network_state
    .peer_registry.read()
    .get_peer(self.peer)
    .and_then(|p| p.connected_addr.peer_id());
if sender_peer_id.as_ref() != Some(&content.from) {
    return StatusCode::InvalidFromPeerId
        .with_context("from field does not match sender identity");
}
```

Alternatively, derive `content.from` from the session's authenticated peer ID rather than trusting the message field.

---

### Proof of Concept

```
State setup:
  victim.inflight_requests.insert(target_peer_id, now)   // simulates notify()

Attack message (sent by attacker over a direct session to victim):
  ConnectionRequestDelivered {
    from:         victim_peer_id,   // spoofed — victim's own PeerId
    to:           target_peer_id,   // observed from gossip
    route:        [],               // empty → triggers the self_peer_id == from branch
    listen_addrs: [attacker_tcp_addr],
    sync_route:   [],
  }

Expected (buggy) outcome:
  1. victim.inflight_requests.remove(target_peer_id) → Some(start)  ← entry evicted
  2. victim sends ConnectionSync to attacker's session
  3. victim spawns try_nat_traversal to attacker_tcp_addr for 30s

Legitimate message arrives:
  ConnectionRequestDelivered { from: victim_peer_id, to: target_peer_id, ... }
  → inflight_requests.remove(target_peer_id) → None
  → returns StatusCode::Ignore  ← NAT traversal permanently aborted
```

### Citations

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

**File:** network/src/protocols/hole_punching/mod.rs (L223-235)
```rust
                    // Broadcast to a number of nodes equal to the square root of the total connection count using gossip.
                    let mut total = status.total.isqrt();
                    let _ignore = context
                        .filter_broadcast(
                            TargetSession::Filter(Box::new(move |_| {
                                total = total.saturating_sub(1);
                                total != 0
                            })),
                            proto_id,
                            conn_req.as_bytes(),
                        )
                        .await;
                    inflight.push(to_peer_id);
```

**File:** network/src/protocols/hole_punching/mod.rs (L239-242)
```rust
            let now = unix_time_as_millis();
            for peer_id in inflight {
                self.inflight_requests.insert(peer_id, now);
            }
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L38-40)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L150-160)
```rust
                let self_peer_id = self.protocol.network_state.local_peer_id();
                if self_peer_id != &content.from {
                    // forward the message to the `from` peer
                    self.forward_delivered(&content.from).await
                } else {
                    // the current peer is the target peer, respond the sync back
                    if let Some(metrics) = ckb_metrics::handle() {
                        metrics.ckb_hole_punching_active_count.inc();
                    }

                    let request_start = self.protocol.inflight_requests.remove(&content.to);
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L162-175)
```rust
                    match request_start {
                        Some(start) => {
                            let res = self.respond_sync(content.from).await;
                            if !res.is_ok() {
                                return res;
                            }
                            let now = unix_time_as_millis();
                            let ttl = now - start;

                            self.try_nat_traversal(ttl, content.listen_addrs);

                            Status::ok()
                        }
                        None => StatusCode::Ignore.with_context("the request is not in flight"),
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L215-234)
```rust
    async fn respond_sync(&self, from_peer_id: PeerId) -> Status {
        let content = init_sync(self.message);
        let new_message = packed::HolePunchingMessage::new_builder()
            .set(content)
            .build()
            .as_bytes();
        let proto_id = SupportProtocols::HolePunching.protocol_id();
        debug!(
            "current peer is the target peer {}, respond the sync back",
            from_peer_id
        );
        if let Err(error) = self
            .p2p_control
            .send_message_to(self.peer, proto_id, new_message)
            .await
        {
            StatusCode::ForwardError.with_context(error)
        } else {
            Status::ok()
        }
```

**File:** network/src/protocols/hole_punching/status.rs (L99-112)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        let code = self.code() as u16;
        if (400..500).contains(&code) {
            Some(BAD_MESSAGE_BAN_TIME)
        } else {
            None
        }
    }

    /// Whether a warning log should be output.
    pub fn should_warn(&self) -> bool {
        let code = self.code() as u16;
        (500..600).contains(&code)
    }
```
