### Title
Rate-Limiter Bypass via Spoofed `from` Peer Identity in Hole-Punching Protocol — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

The hole-punching protocol's `ConnectionRequestProcess` and `ConnectionRequestDeliveredProcess` handlers rate-limit forwarded messages using the **message-payload-supplied** `from` peer ID rather than the **authenticated network identity** of the actual sender (`PeerIndex`). Because `from` is fully attacker-controlled, any connected peer can bypass the rate limiter entirely by cycling through arbitrary fake `from` values, causing unbounded message forwarding to the rest of the network.

---

### Finding Description

In `ConnectionRequestProcess::execute()`, the rate limiter is keyed on `(content.from, content.to, self.msg_item_id)`:

```rust
// connection_request.rs lines 132-143
if self
    .protocol
    .forward_rate_limiter
    .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
    .is_err()
{ ... return StatusCode::TooManyRequests ... }
```

`content.from` is deserialized directly from the untrusted wire message:

```rust
// connection_request.rs lines 36-38
let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
    StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
})?;
```

The struct also carries the **authenticated** sender identity:

```rust
// connection_request.rs lines 85-91
pub(crate) struct ConnectionRequestProcess<'a> {
    ...
    peer: PeerIndex,   // ← real, authenticated sender
    ...
}
```

There is **no check** that `content.from` equals the actual peer's authenticated identity. The same flaw is present in `ConnectionRequestDeliveredProcess::execute()` at the same rate-limiter call site.

The analog to the `OmoAgent.sol` bug is exact: just as `onlyAgent` checked `msg.sender == agents[_id]` (the wrong identity) instead of the entry point, here the rate limiter checks `content.from` (the wrong, attacker-supplied identity) instead of the authenticated `peer`.

---

### Impact Explanation

An attacker who is a connected peer can:

1. Send a stream of `ConnectionRequest` (or `ConnectionRequestDelivered`) messages, each with a freshly generated random `from` peer ID.
2. Every message produces a unique `(from, to, msg_item_id)` key, so `check_key` never returns an error.
3. The receiving node forwards each message toward `content.to`, amplifying the traffic to every intermediate relay node.
4. This constitutes an **unbounded network-level amplification attack**: one attacker connection fans out to the entire hole-punching relay graph, exhausting CPU, memory, and bandwidth on all participating nodes.

**Impact: Medium** — sustained resource exhaustion / network flooding reachable from a single unprivileged peer connection.

---

### Likelihood Explanation

Any peer that can establish a standard P2P connection to a CKB node can trigger this. No special privileges, keys, or majority hash power are required. The hole-punching protocol is enabled by default and the `ConnectionRequest` message is accepted from any connected peer.

**Likelihood: Medium** — straightforward to exploit; requires only a valid peer connection.

---

### Recommendation

Replace the rate-limiter key's `from` component with the **authenticated** sender identity. For the first hop (where `content.route` is empty), enforce that `content.from` matches the actual sender's peer ID before forwarding. Concretely, in `ConnectionRequestProcess::execute()`:

```rust
// Verify the claimed `from` matches the actual authenticated sender
let sender_peer_id = self.protocol.network_state
    .with_peer_registry(|reg| reg.get_peer(self.peer).map(|p| p.peer_id.clone()));
if let Some(sender_id) = sender_peer_id {
    if content.route.is_empty() && sender_id != content.from {
        return StatusCode::InvalidFromPeerId
            .with_context("from field does not match authenticated sender");
    }
}
// Key the rate limiter on the real sender, not the claimed from
self.protocol.forward_rate_limiter
    .check_key(&(sender_id, content.to.clone(), self.msg_item_id))
```

For forwarded messages (non-empty `route`), the rate limiter should key on the **actual forwarding peer** (`self.peer`) rather than the claimed originator.

---

### Proof of Concept

1. Attacker connects to a target CKB node as a normal P2P peer.
2. Attacker sends a sequence of `HolePunchingMessage::ConnectionRequest` messages, each with a distinct random 32-byte `from` field (valid `PeerId` encoding), a fixed `to` field pointing to any peer ID, `max_hops = MAX_HOPS`, and a valid `listen_addrs` list.
3. Each message passes `check_key` because `(random_from_i, to, msg_item_id)` is unique for every `i`.
4. The node calls `forward_message(self_peer_id, &content.to)` for each, broadcasting to all connected peers.
5. Each relay node repeats the same forwarding without rate-limiting (same bypass applies), causing exponential message amplification across the network.

**Root cause lines:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

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
