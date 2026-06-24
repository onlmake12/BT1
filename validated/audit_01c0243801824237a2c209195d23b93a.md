Audit Report

## Title
Unverified `from` Field in Hole-Punching Handlers Enables Rate-Limit Bypass and `pending_delivered` State Poisoning — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary

`ConnectionRequestProcess::execute()` parses `content.from` exclusively from the attacker-controlled wire message and never validates it against the authenticated session identity (`self.peer`). This allows any connected peer to rotate arbitrary fake `from` values to bypass the `forward_rate_limiter` (amplifying forwarded traffic 30× toward any target), and to poison `pending_delivered` with a victim's peer ID, silently blocking that victim's legitimate hole-punching requests through the relay for up to two minutes — renewable indefinitely.

## Finding Description

**Root cause — `from` is taken from message content, not the authenticated session**

`RequestContent::try_from` parses `from` directly from the wire message bytes with no cross-check against the session: [1](#0-0) 

`ConnectionRequestProcess` holds `self.peer: PeerIndex` (the verified session handle) and `self.protocol` (which has access to `network_state.peer_registry`), making the actual peer's `PeerId` reachable. However, `execute()` never compares `content.from` against the real session identity: [2](#0-1) 

**Attack surface 1 — `forward_rate_limiter` bypass**

The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)`: [3](#0-2) 

The limiter is configured at 1 req/s per `(from, to, item_id)` tuple: [4](#0-3) 

The outer per-session `rate_limiter` (keyed by `(session_id, msg.item_id())`) caps the attacker at 30 req/s from a single connection: [5](#0-4) 

Because `content.from` is attacker-controlled, an attacker can rotate through 30 distinct fake `from` peer IDs per second, each producing a fresh bucket in the `HashMapStateStore`. All 30 req/s pass the `forward_rate_limiter` and are forwarded to the target — instead of the intended 1 req/s cap. The relay node becomes a 30× amplifier toward any `to` peer.

**Attack surface 2 — `pending_delivered` state poisoning**

When the relay node is itself the `to` target (`self_peer_id == content.to`), it calls `respond_delivered(content.from, …)`: [6](#0-5) 

Inside `respond_delivered`, the node checks `pending_delivered[from_peer_id]` and silently ignores any repeat within `HOLE_PUNCHING_INTERVAL` (2 minutes): [7](#0-6) 

After sending the response back to `self.peer` (the actual connected session, not the forged `from`), it inserts the forged identity into the map: [8](#0-7) 

An attacker connected to relay R sends a `ConnectionRequest` with `from = victim_peer_id`, `to = R_peer_id`, and valid TCP `listen_addrs`. R inserts `pending_delivered[victim_peer_id] = (attacker_addrs, now)`. For the next 2 minutes, any legitimate `ConnectionRequest` from the real victim to R is silently dropped with `StatusCode::Ignore`. The victim receives no error; the hole-punching attempt simply fails.

**Attack surface 3 — `ConnectionRequestDelivered` in-flight session cancellation**

The same unverified-`from` pattern exists in `ConnectionRequestDeliveredProcess`. When `route` is empty and `self_peer_id == content.from`, the node removes `inflight_requests[content.to]`: [9](#0-8) 

An attacker who knows the relay's public peer ID (trivially discoverable via the identify protocol) can forge `from = relay_peer_id` to cancel any of its in-flight hole-punching sessions.

The `forward_rate_limiter` bypass is also present in `ConnectionRequestDeliveredProcess`: [10](#0-9) 

## Impact Explanation

The rate-limit bypass enables a single connected attacker to flood any target peer with up to 30 forwarded `ConnectionRequest` messages per second (30× the intended 1 req/s cap) through any relay node, with negligible cost. This matches the **High** impact category: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* The `pending_delivered` poisoning compounds this by enabling targeted, renewable DoS against any peer's NAT traversal capability.

## Likelihood Explanation

- **Precondition**: A single authenticated P2P connection to any CKB node with `HolePunching` enabled. No keys, no privileged access, no hash power required.
- **Peer IDs are public**: Every node broadcasts its peer ID via the identify protocol, so victim and relay peer IDs are trivially discoverable.
- **No cryptographic barrier**: The `from` field is a raw byte sequence; forging it requires no key material.
- **Hole-punching is enabled by default** when the feature is compiled in.
- **Renewable**: The `pending_delivered` poisoning can be refreshed every 2 minutes indefinitely.

## Recommendation

In `ConnectionRequestProcess::execute()`, after parsing `content`, look up the actual `PeerId` of `self.peer` from the peer registry and assert it equals `content.from`:

```rust
let actual_from = self.protocol.network_state
    .with_peer_registry(|reg| reg.get_peer(self.peer).map(|p| p.peer_id.clone()));
match actual_from {
    Some(id) if id == content.from => { /* proceed */ }
    _ => return StatusCode::InvalidFromPeerId.with_context("from does not match sender"),
}
```

Apply the same fix to `ConnectionRequestDeliveredProcess` and `ConnectionSyncProcess`. The `forward_rate_limiter` key should then use the verified session identity rather than the message-supplied `content.from`, or the `from` field should be dropped from the message entirely and reconstructed from the session on each hop.

## Proof of Concept

**Setup**: Attacker A is connected to relay node R. Victim V is behind NAT and depends on hole-punching through R.

**Step 1 — `pending_delivered` poisoning**:
1. A learns `R_id` and `V_id` from the identify protocol.
2. A sends a `ConnectionRequest` to R with `from = V_id`, `to = R_id`, `listen_addrs = [A's valid TCP address]`, `max_hops = 1`.
3. R parses `content.from = V_id`, passes the `forward_rate_limiter` check (fresh bucket), calls `respond_delivered(V_id, …)`, sends the delivered response back to A's session, and inserts `pending_delivered[V_id] = (A_addrs, now)`.

**Step 2 — Victim is blocked**:
4. V sends a legitimate `ConnectionRequest` to R with `from = V_id`, `to = R_id`.
5. R calls `respond_delivered(V_id, …)`, finds `pending_delivered[V_id]` with timestamp < 2 minutes ago, returns `StatusCode::Ignore`. V's request is silently dropped.
6. A repeats Step 1 every ~2 minutes to maintain the block indefinitely.

**Step 3 — Rate-limit bypass for amplification**:
7. A sends `ConnectionRequest` messages to R with `from = random_peer_id_1`, `from = random_peer_id_2`, … each targeting `to = V_id`, at up to 30 req/s (bounded only by the outer per-session limiter).
8. Each message creates a new bucket in `forward_rate_limiter`, bypassing the 1 req/s cap. R forwards all of them toward V, flooding V with `ConnectionRequestDelivered` messages.

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L85-108)
```rust
pub(crate) struct ConnectionRequestProcess<'a> {
    message: packed::ConnectionRequestReader<'a>,
    protocol: &'a mut HolePunching,
    peer: PeerIndex,
    p2p_control: &'a ServiceAsyncControl,
    msg_item_id: u32,
}

impl<'a> ConnectionRequestProcess<'a> {
    pub(crate) fn new(
        message: packed::ConnectionRequestReader<'a>,
        protocol: &'a mut HolePunching,
        peer: PeerIndex,
        p2p_control: &'a ServiceAsyncControl,
        msg_item_id: u32,
    ) -> Self {
        Self {
            message,
            protocol,
            peer,
            p2p_control,
            msg_item_id,
        }
    }
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
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

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
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
