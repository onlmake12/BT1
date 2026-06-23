### Title
Unvalidated `from` Peer ID in Hole-Punching `ConnectionRequest` Enables Rate-Limiter Bypass and Peer Identity Spoofing — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

The `ConnectionRequestProcess::execute()` handler in the CKB hole-punching protocol parses the `from` peer ID directly from the attacker-controlled message payload instead of deriving it from the authenticated peer connection. This `from` value is then used as the key for the per-`(from, to)` rate limiter and as the claimed originator in forwarded `ConnectionRequestDelivered` messages. An unprivileged peer can rotate arbitrary fake `from` peer IDs across successive messages to exhaust rate-limit buckets and cause the node to forward an unbounded volume of hole-punching traffic to other peers, constituting a traffic-amplification DoS. The same flaw exists in `ConnectionRequestDeliveredProcess`.

---

### Finding Description

**Root cause — `connection_request.rs`**

`RequestContent` is deserialized entirely from the wire message:

```rust
// network/src/protocols/hole_punching/component/connection_request.rs  L36-38
let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
    StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
})?;
```

The struct `ConnectionRequestProcess` carries the *actual* peer index of the sender as `self.peer` (line 88), but `content.from` is never cross-checked against the peer identity that the transport layer authenticated. [1](#0-0) 

**Rate-limiter keyed on attacker-controlled value**

```rust
// L132-143
if self
    .protocol
    .forward_rate_limiter
    .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
    .is_err()
{ ... return StatusCode::TooManyRequests ... }
```

Because `content.from` is attacker-supplied, the attacker can generate a fresh `(from, to, msg_item_id)` tuple for every message, each landing in its own rate-limit bucket. The limiter never fires. [2](#0-1) 

**Forwarding path uses spoofed identity**

When `self_peer_id != &content.to` and `content.max_hops > 0`, the node calls `self.forward_message(...)`, relaying the message (with the forged `from`) to another peer. [3](#0-2) 

When `self_peer_id == &content.to`, the node calls `self.respond_delivered(content.from, ...)`, embedding the spoofed peer ID into the outbound `ConnectionRequestDelivered` message sent back to the attacker. [4](#0-3) 

**Same flaw in `ConnectionRequestDeliveredProcess`**

`DeliverdContent.from` is again parsed from the wire message and used as the rate-limiter key without validation against the actual sending peer. [5](#0-4) 

The delivered handler then uses `content.from` to decide whether to forward or to trigger NAT traversal: [6](#0-5) 

---

### Impact Explanation

1. **Rate-limiter bypass / traffic amplification DoS**: A single attacker peer can send an unlimited stream of `ConnectionRequest` messages, each with a distinct fake `from` peer ID. Every message passes the rate-limit check (new bucket each time) and causes the node to forward the message to another peer. The attacker's outbound bandwidth is amplified across the node's outbound connections, degrading the node's network resources and those of its peers.

2. **Peer identity spoofing in hole-punching state machine**: The attacker can forge `ConnectionRequest` messages claiming to originate from any peer ID (e.g., a well-known node). The target node will embed that forged identity in `ConnectionRequestDelivered` responses and in subsequent `ConnectionSync` messages, corrupting the hole-punching state machine for legitimate peers and potentially causing spurious NAT-traversal attempts to attacker-chosen addresses.

---

### Likelihood Explanation

Any unprivileged peer connected to a CKB node running the `HolePunching` protocol can trigger this immediately. No keys, no special role, and no majority hash power are required. The attacker needs only a single TCP connection to the node. The hole-punching protocol is an opt-in network feature but is enabled in production builds.

---

### Recommendation

After deserializing `content.from`, validate it against the authenticated peer identity of the actual sender:

```rust
// Resolve the PeerId of self.peer from the network state
let actual_peer_id = self.protocol.network_state
    .with_peer_registry(|reg| reg.get_peer(self.peer)
        .and_then(|p| extract_peer_id(&p.connected_addr)));

if actual_peer_id.as_ref() != Some(&content.from) {
    return StatusCode::InvalidFromPeerId
        .with_context("from peer id does not match the actual sender");
}
```

Apply the same fix in `ConnectionRequestDeliveredProcess`. This mirrors the fix recommended in the DAOfi report: use the authenticated caller identity (`msg.sender` / actual peer) rather than the caller-supplied parameter (`lp.sender` / `content.from`).

---

### Proof of Concept

1. Attacker establishes a valid P2P connection to a CKB node (standard peer connection, no privileges).
2. Attacker sends a rapid sequence of `ConnectionRequest` messages over the `HolePunching` protocol, each with a distinct randomly-generated `from` peer ID and a fixed `to` peer ID (e.g., a known node).
3. Each message passes `forward_rate_limiter.check_key(...)` because each `(from, to, msg_item_id)` tuple is unique.
4. For each message where `max_hops > 0` and the node is not the target, the node calls `forward_message`, relaying the crafted packet to another connected peer.
5. The attacker's single connection generates O(N) forwarded messages per second, where N is limited only by the attacker's send rate — the intended rate limiter provides no protection. [7](#0-6) [8](#0-7)

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L85-91)
```rust
pub(crate) struct ConnectionRequestProcess<'a> {
    message: packed::ConnectionRequestReader<'a>,
    protocol: &'a mut HolePunching,
    peer: PeerIndex,
    p2p_control: &'a ServiceAsyncControl,
    msg_item_id: u32,
}
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L110-152)
```rust
    pub(crate) async fn execute(mut self) -> Status {
        let content = match RequestContent::try_from(&self.message) {
            Ok(content) => content,
            Err(status) => return status,
        };
        if content.listen_addrs.len() > ADDRS_COUNT_LIMIT || content.listen_addrs.is_empty() {
            return StatusCode::InvalidListenAddrLen
                .with_context("the listen address count is too large or empty");
        }

        if content.max_hops > MAX_HOPS {
            return StatusCode::InvalidMaxTTL.into();
        }
        if content.route.len() > MAX_HOPS as usize {
            return StatusCode::InvalidRoute.with_context("the route length is too long");
        }

        let self_peer_id = self.protocol.network_state.local_peer_id();
        if content.route.contains(self_peer_id) {
            return StatusCode::Ignore.with_context("the message is passed, ignore it");
        }

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

        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
        } else if content.max_hops == 0u8 {
            StatusCode::ReachedMaxHops.into()
        } else {
            self.forward_message(self_peer_id, &content.to).await
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L38-40)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L120-145)
```rust
    pub(crate) async fn execute(self) -> Status {
        let content = match DeliverdContent::try_from(&self.message) {
            Ok(content) => content,
            Err(status) => return status,
        };
        if content.listen_addrs.len() > ADDRS_COUNT_LIMIT || content.listen_addrs.is_empty() {
            return StatusCode::InvalidListenAddrLen
                .with_context("the listen address count is too large or empty");
        }

        if content.route.len() > MAX_HOPS as usize || content.sync_route.len() > MAX_HOPS as usize {
            return StatusCode::InvalidRoute.with_context("the route length is too long");
        }

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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L147-177)
```rust
        match content.route.last() {
            Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
            None => {
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
                    }
                }
```
