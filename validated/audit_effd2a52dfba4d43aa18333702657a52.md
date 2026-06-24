Audit Report

## Title
Missing `from` Peer ID Validation Enables Rate-Limiter Bypass and Gossip Amplification in Hole Punching Protocol — (File: `network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary

`ConnectionRequestProcess::execute()` parses `content.from` from the attacker-controlled wire message but never validates it against the authenticated session's peer ID. This allows any connected peer to spoof arbitrary `from` identities, bypass the per-`(from,to)` `forward_rate_limiter`, and trigger `sqrt(N)`-fan-out gossip broadcasts at 30 msg/sec — the only binding throttle being the outer per-session rate limiter. A secondary consequence is `pending_delivered` map pollution that silently blocks legitimate hole-punch responses for innocent peers for up to 2 minutes per injection.

## Finding Description

**Root cause — no `from` authentication:**

In `execute()`, `content.from` is deserialized directly from the wire payload: [1](#0-0) 

`self.peer` (the authenticated `PeerIndex` / session ID) is stored in the struct but is never resolved to a `PeerId` and compared against `content.from`. All downstream logic trusts the attacker-supplied identity.

**Rate-limiter bypass:**

The `forward_rate_limiter` is keyed on the attacker-controlled tuple `(content.from, content.to, msg_item_id)`: [2](#0-1) 

The limiter is a `HashMap`-backed keyed governor at 1 req/sec per key: [3](#0-2) 

By cycling distinct spoofed `from` peer IDs, each message gets a fresh bucket, rendering the 1-req/sec-per-`(from,to)` cap ineffective. The only real throttle is the outer per-session limiter at 30 req/sec: [4](#0-3) 

**Gossip amplification:**

When `to` is not directly connected, `forward_message` broadcasts to `sqrt(total_peers)` nodes: [5](#0-4) 

`forward_request` appends the current node's peer ID to the route on each hop: [6](#0-5) 

Route deduplication (line 128–130) prevents infinite loops, but with `max_hops=6` and `sqrt(N)` fan-out per hop, a single attacker session at 30 msg/sec produces up to `30 × sqrt(N)` forwarded messages per second at the first relay tier alone.

**`pending_delivered` pollution:**

When the relay is the `to` target, it checks and then inserts the attacker-supplied `from_peer_id` into `pending_delivered`: [7](#0-6) [8](#0-7) 

Any subsequent legitimate `ConnectionRequest` with `from=INNOCENT_PEER_ID` arriving within `HOLE_PUNCHING_INTERVAL` (2 minutes) is silently dropped: [9](#0-8) 

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

The `forward_rate_limiter` is the primary defense against relay-amplified flooding. Its bypass means the effective forwarding rate jumps from 1 req/sec per `(from,to)` pair to 30 req/sec per session. With `sqrt(N)` fan-out per hop and `max_hops=6`, a single low-cost attacker session can inject a disproportionate volume of forwarded `ConnectionRequest` messages across the P2P overlay. Multiple attacker sessions compound this linearly. The secondary `pending_delivered` pollution disrupts hole-punching for targeted innocent peers for 2-minute windows per injection, degrading NAT traversal availability.

## Likelihood Explanation

Requires only a single authenticated P2P connection to any relay node — no special privileges, no PoW, no key material. The attacker constructs well-formed `ConnectionRequest` messages with arbitrary `from` bytes fields. The attack is fully local-testable, repeatable indefinitely, and trivially scriptable. The outer 30 req/sec per-session cap is the only real constraint, and it is easily saturated.

## Recommendation

In `execute()`, resolve the actual `PeerId` for `self.peer` from the peer registry and assert it equals `content.from` before proceeding. Reject and ban the session if they differ:

```rust
let actual_peer_id = self.protocol.network_state
    .with_peer_registry(|r| r.get_peer(self.peer).map(|p| p.peer_id.clone()));
if actual_peer_id.as_ref() != Some(&content.from) {
    return StatusCode::InvalidFromPeerId
        .with_context("from does not match authenticated session peer id");
}
```

`StatusCode::InvalidFromPeerId` is in the `4xx` range and will trigger the existing ban logic in `received()`: [10](#0-9) [11](#0-10) 

## Proof of Concept

```
1. Attacker connects to relay node R (session authenticated as peer_id=ATTACKER).
2. Attacker sends 30 ConnectionRequest messages per second, each with:
       from = INNOCENT_PEER_ID_<i>,   // distinct spoofed value per message
       to   = SOME_UNKNOWN_PEER,
       listen_addrs = [valid_tcp_addr],
       max_hops = 6,
       route = [],
3. execute() parses content.from = INNOCENT_PEER_ID_<i> without checking self.peer.
4. forward_rate_limiter checks (INNOCENT_PEER_ID_<i>, SOME_UNKNOWN_PEER, item_id) —
   each is a fresh bucket, so all 30 pass per second.
5. SOME_UNKNOWN_PEER not in peer registry → filter_broadcast to sqrt(N) peers each time.
6. Relay emits 30 × sqrt(N) forwarded messages/sec from one attacker session.
7. Each receiving relay repeats the same fan-out (bounded by route dedup + max_hops=6).

To poison pending_delivered:
8. Attacker sends from=INNOCENT_PEER_ID, to=R's own peer ID.
9. R stores INNOCENT_PEER_ID in pending_delivered; legitimate hole-punch requests
   from that peer are silently ignored for 2 minutes per injection.

Minimal unit test: construct a mock HolePunching instance, call execute() with
content.from != session peer_id, assert forward_rate_limiter is checked against
the spoofed key and that pending_delivered is polluted.
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

**File:** network/src/protocols/hole_punching/mod.rs (L24-24)
```rust
pub(crate) const HOLE_PUNCHING_INTERVAL: u64 = 2 * 60 * 1000; // 2 minutes
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

**File:** network/src/protocols/hole_punching/mod.rs (L255-257)
```rust
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
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

**File:** network/src/protocols/hole_punching/status.rs (L99-106)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        let code = self.code() as u16;
        if (400..500).contains(&code) {
            Some(BAD_MESSAGE_BAN_TIME)
        } else {
            None
        }
    }
```
