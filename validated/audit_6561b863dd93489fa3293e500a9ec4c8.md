### Title
Unverified `from` Peer ID in Hole Punching `ConnectionRequest` Enables `pending_delivered` Cache Poisoning and Forced Outbound Connections to Attacker-Controlled Addresses — (`File: network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

The CKB hole punching protocol handler accepts a `ConnectionRequest` message whose `from` field is taken directly from the attacker-controlled message payload and used as the authoritative sender identity — without ever verifying it against the cryptographically authenticated session peer ID. This is the direct analog to using `msg.sender` instead of `msgSender()` in a metatransaction context: the real, authenticated identity is available but ignored in favor of the payload-supplied one. The consequence is two-fold: the `forward_rate_limiter` can be bypassed by rotating spoofed `from` values, and — more critically — the `pending_delivered` cache can be poisoned with attacker-controlled listen addresses, which are later consumed by `ConnectionSync` to force the victim node to make outbound TCP connections to arbitrary attacker-controlled IP:port pairs.

---

### Finding Description

In `network/src/protocols/hole_punching/component/connection_request.rs`, `RequestContent::try_from` parses `content.from` entirely from the wire message:

```rust
let from = PeerId::from_bytes(value.from().raw_data().to_vec())...
``` [1](#0-0) 

The `ConnectionRequestProcess` struct holds `self.peer: PeerIndex` — the actual authenticated session index — but `execute()` never resolves this to a `PeerId` and never checks that `content.from` matches the real session peer ID. [2](#0-1) 

**Impact 1 — `forward_rate_limiter` bypass.** The limiter is keyed on `(content.from, content.to, msg_item_id)`:

```rust
self.protocol
    .forward_rate_limiter
    .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
``` [3](#0-2) 

Because `content.from` is attacker-supplied, an attacker can rotate a fresh random `from` peer ID in every message, generating a new rate-limiter bucket each time and forwarding an unlimited number of `ConnectionRequest` messages toward any `to` target, amplifying traffic across the network.

**Impact 2 — `pending_delivered` cache poisoning → forced outbound TCP connections.** When the receiving node is the `to` target, `respond_delivered` is called with the attacker-supplied `from_peer_id` and the attacker-supplied `remote_listens`:

```rust
self.protocol
    .pending_delivered
    .insert(from_peer_id, (remote_listens, now));
``` [4](#0-3) 

Later, `ConnectionSyncProcess::execute()` looks up `pending_delivered` by `content.from` (again, attacker-supplied from the sync message payload):

```rust
let listens_info = self
    .protocol
    .pending_delivered
    .get(&content.from)
    .map(|info| info.0.clone());
``` [5](#0-4) 

If the lookup succeeds, the node spawns async tasks calling `try_nat_traversal` for each stored address:

```rust
let tasks = listens
    .into_iter()
    .map(|listen_addr| {
        Box::pin(try_nat_traversal(self.bind_addr, listen_addr))
    })
    .collect::<Vec<_>>();
``` [6](#0-5) 

This causes the victim node to make real outbound TCP connections to the attacker-controlled IP:port values that were stored in `pending_delivered`.

The outer per-session `rate_limiter` (keyed on `(session_id, msg_item_id)`) does not prevent this because it only limits the rate per session, not the content of what gets stored. [7](#0-6) 

---

### Impact Explanation

An unprivileged peer with a single authenticated session can:

1. **Bypass the `forward_rate_limiter`** by sending `ConnectionRequest` messages with a different spoofed `from` peer ID each time, causing the victim node to forward an unbounded number of messages toward arbitrary `to` targets — amplifying network load.

2. **Force the victim node to make outbound TCP connections to arbitrary attacker-controlled IP:port pairs** by poisoning `pending_delivered` via a spoofed `ConnectionRequest` (step 1), then triggering the lookup via a spoofed `ConnectionSync` (step 2). This enables:
   - Internal/external port scanning from the victim node's IP
   - Connecting to attacker-controlled infrastructure (potential for further exploitation of the raw TCP stream)
   - Resource exhaustion via many concurrent `try_nat_traversal` tasks

---

### Likelihood Explanation

The hole punching protocol is reachable by any connected peer. No privilege, key, or special role is required — a single inbound or outbound connection suffices. The `from` field is a free-form byte sequence in the molecule-encoded message; any peer can set it to any value. The attack requires only two messages (`ConnectionRequest` then `ConnectionSync`) and is fully deterministic.

---

### Recommendation

Before using `content.from` for any security-relevant purpose (rate limiting, cache insertion), verify that it matches the actual authenticated session peer ID. The session peer ID can be resolved from `self.peer` (the `PeerIndex`) via the peer registry:

```rust
let actual_from = self
    .protocol
    .network_state
    .with_peer_registry(|reg| {
        reg.get_peer(self.peer)
            .and_then(|p| extract_peer_id(&p.connected_addr))
    });

if actual_from.as_ref() != Some(&content.from) {
    return StatusCode::InvalidFromPeerId
        .with_context("from peer id does not match session peer id");
}
```

This mirrors the fix recommended in the original report: always use the transport-authenticated identity rather than the payload-supplied one.

---

### Proof of Concept

1. Attacker establishes a valid P2P session with victim node V (peer index `P`).
2. Attacker sends a `HolePunchingMessage::ConnectionRequest` with:
   - `from` = arbitrary fake `PeerId` F (not the attacker's real peer ID)
   - `to` = V's own peer ID (obtainable from the identify protocol)
   - `listen_addrs` = `[/ip4/192.168.1.1/tcp/9999]` (attacker-controlled address)
   - `max_hops` = 1, `route` = []
3. V's `ConnectionRequestProcess::execute()` sees `self_peer_id == &content.to`, calls `respond_delivered(F, V, [192.168.1.1:9999])`.
4. V stores `pending_delivered[F] = ([192.168.1.1:9999], now)`.
   - `content.from` (= F) is never checked against the real peer ID of session `P`.
5. Attacker sends a `HolePunchingMessage::ConnectionSync` with `from` = F, `to` = V's peer ID, `route` = [].
6. V's `ConnectionSyncProcess::execute()` sees `self_peer_id == &content.to`, looks up `pending_delivered[F]`, finds `[192.168.1.1:9999]`, and spawns `try_nat_traversal(bind_addr, /ip4/192.168.1.1/tcp/9999)`.
7. V makes an outbound TCP connection to `192.168.1.1:9999` — an address entirely controlled by the attacker. [8](#0-7) [9](#0-8)

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L35-38)
```rust
    fn try_from(value: &packed::ConnectionRequestReader<'_>) -> Result<Self, Self::Error> {
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-153)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
        } else if content.max_hops == 0u8 {
            StatusCode::ReachedMaxHops.into()
        } else {
            self.forward_message(self_peer_id, &content.to).await
        }
    }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L100-124)
```rust
            None => {
                let self_peer_id = self.protocol.network_state.local_peer_id();
                if self_peer_id != &content.to {
                    // forward the message to the `to` peer
                    self.forward_sync(&content.to).await
                } else {
                    // Current node should be the `to` target.
                    if let Some(metrics) = ckb_metrics::handle() {
                        metrics.ckb_hole_punching_passive_count.inc();
                    }

                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());

                    match listens_info {
                        Some(listens) => {
                            let tasks = listens
                                .into_iter()
                                .map(|listen_addr| {
                                    Box::pin(try_nat_traversal(self.bind_addr, listen_addr))
                                })
                                .collect::<Vec<_>>();
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
