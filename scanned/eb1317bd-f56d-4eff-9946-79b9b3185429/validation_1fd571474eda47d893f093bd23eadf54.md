### Title
Unauthenticated `from` Field in `ConnectionRequest` Enables Identity Spoofing and Forward Rate-Limiter Bypass — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

`ConnectionRequestProcess::execute()` never validates that `content.from` matches the authenticated peer ID of the actual sending session. An attacker with a single connection to a relay node can set `from` to any arbitrary peer ID, causing the relay to forward messages attributed to spoofed identities and bypassing the per-`(from, to)` forward rate limiter by rotating spoofed values.

---

### Finding Description

In `execute()`, the `from` field is parsed directly from the attacker-controlled message payload: [1](#0-0) 

The actual sending session is available as `self.peer` (a `PeerIndex`), but it is **never compared** against `content.from`. The code proceeds to use `content.from` as the authoritative identity of the requester for all downstream logic.

The `forward_rate_limiter` is keyed on the spoofed `content.from`: [2](#0-1) 

By rotating `content.from` across different spoofed peer IDs, the attacker exhausts a fresh rate-limit bucket for each unique `(from, to)` pair, effectively bypassing the 1-req/s-per-pair forward rate limiter entirely. The only real constraint is the session-level limiter (30 req/s), keyed on the actual `session_id`: [3](#0-2) 

When `to` is not directly connected, `forward_message()` broadcasts to `sqrt(total_peers)` peers, amplifying each attacker message: [4](#0-3) 

The forwarded message carries the spoofed `from` intact — `forward_request()` only appends the relay's own ID to the `route` field, leaving `from` unchanged: [5](#0-4) 

Additionally, when the relay node is the `to` target, `respond_delivered()` stores `content.from` (the spoofed ID) into `pending_delivered`, poisoning the relay's NAT traversal state with attacker-controlled peer identities: [6](#0-5) 

---

### Impact Explanation

1. **Forward rate-limiter bypass**: Rotating spoofed `from` values creates fresh rate-limit buckets, allowing up to 30 forwarded messages/second (the session cap) instead of 1/s per pair.
2. **Amplification**: Each forwarded message in broadcast mode reaches `sqrt(N)` peers, so 30 attacker messages/second become `30 × sqrt(N)` network messages/second.
3. **Identity spoofing**: All forwarded messages carry the innocent peer's ID as `from`. Downstream nodes that apply per-`from` policy (rate limiting, banning, `pending_delivered` state) act against the innocent peer's identity, not the attacker's session.
4. **`pending_delivered` state poisoning**: If the relay is the `to` target, the attacker can fill `pending_delivered` with arbitrary spoofed `from` entries, interfering with legitimate hole-punching flows for those peer IDs.

---

### Likelihood Explanation

Requires only a single standard P2P connection to any relay node. No special privileges, keys, or majority hashpower needed. The `ConnectionRequest` message is a normal production P2P message type. The missing check is a single missing equality assertion.

---

### Recommendation

In `execute()`, after parsing `content`, resolve the actual peer ID for `self.peer` from the peer registry and assert it equals `content.from`. If they differ, return a ban-triggering status (4xx):

```rust
let actual_peer_id = self.protocol.network_state
    .with_peer_registry(|reg| reg.get_peer(self.peer).map(|p| p.peer_id.clone()));
if actual_peer_id.as_ref() != Some(&content.from) {
    return StatusCode::InvalidFromPeerId
        .with_context("from peer id does not match the actual sender");
}
```

This check should be applied consistently to `ConnectionRequestDelivered` and `ConnectionSync` as well.

---

### Proof of Concept

1. Attacker connects to relay node R (session `S`), authenticated as peer ID `A`.
2. Attacker sends `ConnectionRequest { from: innocent_peer_id_B, to: target_C, listen_addrs: [...], max_hops: 6, route: [] }`.
3. `execute()` parses `content.from = B`, never checks `B == A`.
4. `forward_rate_limiter.check_key(&(B, C, 0))` passes (fresh bucket for B).
5. `forward_message()` broadcasts the message to `sqrt(N)` peers with `from=B`.
6. Attacker repeats with `from: B2, B3, ...` — each gets a fresh forward rate-limit bucket, all 30 req/s session slots are used for forwarding.
7. Downstream nodes see hole-punch requests attributed to `B`, `B2`, `B3`, etc. — the attacker `A` is never visible in the forwarded messages.

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L110-114)
```rust
    pub(crate) async fn execute(mut self) -> Status {
        let content = match RequestContent::try_from(&self.message) {
            Ok(content) => content,
            Err(status) => return status,
        };
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
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

**File:** network/src/protocols/hole_punching/component/mod.rs (L171-187)
```rust
pub(crate) fn forward_request(
    request: packed::ConnectionRequestReader<'_>,
    current_id: &PeerId,
) -> packed::ConnectionRequest {
    let max_hops: u8 = request.max_hops().into();
    let message = request.to_entity();
    let new_route = message
        .route()
        .as_builder()
        .push(current_id.as_bytes())
        .build();
    message
        .as_builder()
        .max_hops(max_hops.saturating_sub(1))
        .route(new_route)
        .build()
}
```
