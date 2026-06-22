### Title
Missing `from` Peer ID Validation Enables Identity Spoofing and Rate-Limiter Bypass in Hole Punching Protocol — (`network/src/protocols/hole_punching/component/connection_request.rs`)

### Summary

`ConnectionRequestProcess::execute()` parses `content.from` from the attacker-controlled message payload but never validates it against the authenticated session peer ID. This allows any connected peer to spoof arbitrary `from` identities, bypass the per-`(from,to)` forward rate limiter, and use the relay node as a gossip amplifier.

### Finding Description

In `execute()`, `content.from` is taken directly from the wire message: [1](#0-0) 

The `self.peer` field (the actual authenticated `PeerIndex` / session ID) is stored in the struct but is **never compared against `content.from`**. All subsequent logic trusts the attacker-supplied identity.

The `forward_rate_limiter` is then keyed on the attacker-controlled tuple: [2](#0-1) 

Because the key includes `content.from`, an attacker cycling through distinct spoofed `from` peer IDs gets a fresh rate-limit bucket for each one, effectively bypassing the 1-req/sec-per-`(from,to)` cap. The only real throttle is the outer per-session limiter at 30 req/sec: [3](#0-2) 

When the `to` peer is not directly connected, `forward_message` gossip-broadcasts to `sqrt(total_peers)` nodes: [4](#0-3) 

This gives an amplification factor of `sqrt(N)` per message. At 30 msg/sec from one attacker session and 100 connected peers, the relay emits 300 forwarded messages/sec attributed to arbitrary spoofed identities.

Additionally, when the relay node itself is the `to` target, it inserts the spoofed `from_peer_id` into `pending_delivered`: [5](#0-4) 

This blocks any legitimate hole-punch response for the innocent peer for `HOLE_PUNCHING_INTERVAL` (2 minutes): [6](#0-5) 

### Impact Explanation

1. **Identity spoofing**: All forwarded `ConnectionRequest` messages carry the attacker-chosen `from` peer ID, not the actual sender's authenticated identity.
2. **Rate-limiter bypass**: The `forward_rate_limiter` is rendered ineffective because its key includes the attacker-controlled `from` field. The only binding limit is 30 msg/sec per session.
3. **Gossip amplification**: Each spoofed message triggers a `sqrt(N)` broadcast, multiplying the attacker's traffic.
4. **`pending_delivered` pollution / hole-punch DoS**: Spoofing `from=innocent_peer_id` to a relay that is the `to` target poisons the relay's `pending_delivered` map, silently blocking legitimate hole-punch responses for the innocent peer for 2 minutes per injection.

**Correction to the question's claim**: Bans are applied to `session_id` (the actual TCP session), not to `content.from`. The innocent peer is not directly banned; however, their hole-punching capability is disrupted via `pending_delivered` pollution.

### Likelihood Explanation

Requires only a single authenticated P2P connection to any relay node. No special privileges, no PoW, no key material needed. The attacker constructs a well-formed `ConnectionRequest` with an arbitrary `from` bytes field. Fully local-testable.

### Recommendation

In `execute()`, resolve the actual peer ID for `self.peer` from the peer registry and assert it equals `content.from` before proceeding. Reject (and ban) the session if they differ:

```rust
let actual_peer_id = self.protocol.network_state
    .with_peer_registry(|r| r.get_peer(self.peer).map(|p| p.peer_id.clone()));
if actual_peer_id.as_ref() != Some(&content.from) {
    return StatusCode::InvalidFromPeerId
        .with_context("from does not match authenticated session peer id");
}
```

This would return a 4xx status code, triggering the existing ban logic in `received()`. [7](#0-6) 

### Proof of Concept

```
1. Attacker connects to relay node R (session authenticated as peer_id=ATTACKER).
2. Attacker sends: ConnectionRequest {
       from = INNOCENT_PEER_ID,   // spoofed
       to   = SOME_UNKNOWN_PEER,
       listen_addrs = [valid_tcp_addr],
       max_hops = 6,
       route = [],
   }
3. R's execute() parses content.from = INNOCENT_PEER_ID without checking self.peer.
4. forward_rate_limiter is checked against (INNOCENT_PEER_ID, SOME_UNKNOWN_PEER, 0) — fresh bucket.
5. SOME_UNKNOWN_PEER not in peer registry → filter_broadcast to sqrt(N) peers.
6. All sqrt(N) peers receive a ConnectionRequest attributed to INNOCENT_PEER_ID.
7. Attacker repeats with from = INNOCENT_PEER_ID_2, _3, ... — each gets its own rate bucket.
8. To poison pending_delivered: send from=INNOCENT_PEER_ID, to=R's own peer ID.
   R stores INNOCENT_PEER_ID in pending_delivered; legitimate requests from that peer
   are silently ignored for 2 minutes.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L111-114)
```rust
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L161-166)
```rust
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
            }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L280-304)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L145-155)
```rust
        if let Some(ban_time) = status.should_ban() {
            error!(
                "process {} from {}; ban {:?} since result is {}",
                item_name, session_id, ban_time, status
            );
            self.network_state.ban_session(
                &context.control().clone().into(),
                session_id,
                ban_time,
                status.to_string(),
            );
```
