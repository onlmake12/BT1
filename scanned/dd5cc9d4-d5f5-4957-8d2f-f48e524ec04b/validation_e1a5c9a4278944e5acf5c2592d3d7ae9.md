### Title
Unauthenticated `from` Field in `ConnectionRequestDelivered` Bypasses Originator Identity Check, Enabling Inflight-Request Cancellation and Forced NAT Traversal to Attacker-Controlled Addresses — (File: `network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

---

### Summary

The Hole Punching protocol's `ConnectionRequestDelivered` handler uses the attacker-controlled `content.from` field to decide whether the local node is the originator of a hole-punching request. Because `content.from` is never validated against the actual sender's authenticated peer ID, any connected peer can spoof `from = local_peer_id`, bypass the originator check, cancel legitimate inflight hole-punching requests, and force the node to make outbound TCP connections to arbitrary attacker-specified addresses.

---

### Finding Description

The `HolePunching` protocol implements a three-message handshake: `ConnectionRequest` → `ConnectionRequestDelivered` → `ConnectionSync`. When the node receives a `ConnectionRequestDelivered` message and the `route` list is empty, it checks whether it is the originator of the request by comparing its own peer ID against `content.from`:

```rust
// connection_request_delivered.rs, lines 150–153
let self_peer_id = self.protocol.network_state.local_peer_id();
if self_peer_id != &content.from {
    self.forward_delivered(&content.from).await
} else {
    // the current peer is the target peer, respond the sync back
    let request_start = self.protocol.inflight_requests.remove(&content.to);
    match request_start {
        Some(start) => {
            let res = self.respond_sync(content.from).await;
            ...
            self.try_nat_traversal(ttl, content.listen_addrs);
            Status::ok()
        }
        None => StatusCode::Ignore.with_context("the request is not in flight"),
    }
}
```

This `self_peer_id != &content.from` check is the direct analog of the Solidity `onlySelf` modifier. It is intended to restrict the "originator" code path to only execute when the local node genuinely sent the original `ConnectionRequest`. However, `content.from` is parsed directly from the wire message without any cross-check against the actual sender's authenticated session peer ID. The `DeliverdContent::try_from` parser only validates that `from` is a syntactically valid `PeerId`; it does not verify that `from` matches the peer who sent the message:

```rust
// connection_request_delivered.rs, lines 38–40
let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
    StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
})?;
```

The actual sender's peer ID is available as `self.peer` (a `PeerIndex` that maps to an authenticated session), but it is never compared against `content.from`.

The local node's peer ID is public (it is broadcast during the Identify handshake and embedded in multiaddrs). Any connected peer can therefore craft a `ConnectionRequestDelivered` with `from = local_peer_id`, `route = []`, and attacker-chosen `to` and `listen_addrs`, causing the node to enter the originator branch.

---

### Impact Explanation

**1. Inflight hole-punching request cancellation (DoS):**
`self.protocol.inflight_requests.remove(&content.to)` permanently removes the entry for `content.to` from the map. If the local node has an active hole-punching attempt to peer `X`, an attacker who knows `X`'s peer ID (observable from gossip or peer-store queries) can cancel it by sending a spoofed `ConnectionRequestDelivered` with `from = local_peer_id`, `to = X`. The node silently discards the inflight state and the hole-punching attempt is abandoned.

**2. Forced outbound TCP connections to attacker-controlled addresses:**
When `inflight_requests.remove` returns `Some(start)`, the code calls `self.try_nat_traversal(ttl, content.listen_addrs)`. This spawns an async task that makes repeated outbound TCP connection attempts (up to 30 seconds) to each address in `content.listen_addrs`. The attacker controls `content.listen_addrs` (validated only to be syntactically valid TCP multiaddrs with `content.to`'s peer ID, which the attacker also controls). This allows the attacker to direct the victim node to make TCP connections to arbitrary IP:port combinations — enabling port scanning of internal networks, resource exhaustion (each attempt holds a socket for up to 30 s), or amplification of connection load.

**3. Spurious metric increment:**
`metrics.ckb_hole_punching_active_count.inc()` is incremented on every spoofed message, polluting observability data.

---

### Likelihood Explanation

- **Entry path**: Any unprivileged peer connected to the victim node over the P2P network can send a `HolePunchingMessage::ConnectionRequestDelivered`. No special role or key is required.
- **Required knowledge**: The local node's peer ID is public. The `to` peer ID needed to hit an inflight request can be inferred by observing which `ConnectionRequest` messages the victim recently broadcast (they are gossiped to sqrt(N) peers).
- **Rate limiting**: The `forward_rate_limiter` keys on `(content.from, content.to, msg_item_id)`. An attacker can trivially vary `msg_item_id` (it is a `u32` field the attacker controls) or `content.to` to bypass the 1-per-second limit per key.
- **Likelihood**: Medium. The attack requires a connected peer and knowledge of an inflight `to` peer ID for the most impactful effect, but the forced-NAT-traversal primitive is reachable with any valid `to` value as long as an inflight entry exists.

---

### Recommendation

Before entering the originator branch, validate that `content.from` matches the authenticated peer ID of the actual sender. The sender's peer ID can be resolved from `self.peer` (the `PeerIndex`) via the peer registry:

```rust
// Resolve the actual sender's PeerId from the authenticated session
let sender_peer_id = self.protocol.network_state
    .with_peer_registry(|reg| reg.get_peer(self.peer).map(|p| p.connected_addr.clone()));
// Extract peer id from connected_addr and compare against content.from
```

If `content.from` does not match the resolved sender peer ID, the message should be rejected (or treated as a forward, not as a terminal delivery). This mirrors the fix recommended in the report: use the authenticated caller identity rather than the attacker-supplied field.

---

### Proof of Concept

**Setup**: Attacker peer `A` is connected to victim node `V`. `V` has recently broadcast a `ConnectionRequest` to peer `T`, so `V.inflight_requests[T] = timestamp`.

**Step 1**: Attacker `A` crafts a `ConnectionRequestDelivered` molecule message:
- `from` = `V`'s peer ID (public, obtained from Identify protocol or peer-store)
- `to` = `T`'s peer ID (observed from `V`'s gossiped `ConnectionRequest`)
- `route` = `[]` (empty, forces the terminal branch)
- `sync_route` = `[]`
- `listen_addrs` = `[/ip4/192.168.1.1/tcp/9999/p2p/<T_peer_id>]` (attacker-controlled internal address)

**Step 2**: `A` sends this message to `V` over the HolePunching protocol channel.

**Step 3**: `V` processes the message in `ConnectionRequestDeliveredProcess::execute()`:
- `content.route.last()` → `None` (empty route)
- `self_peer_id == &content.from` → **true** (spoofed)
- `inflight_requests.remove(&T)` → `Some(timestamp)` — **inflight request cancelled**
- `respond_sync(V_peer_id)` → sends `ConnectionSync` back to `A`'s session
- `try_nat_traversal(ttl, [/ip4/192.168.1.1/tcp/9999/...])` → **V makes outbound TCP connections to 192.168.1.1:9999 for up to 30 seconds**

**Result**: `V`'s hole-punching attempt to `T` is permanently cancelled, and `V` makes repeated TCP connection attempts to the attacker-specified internal address. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L34-42)
```rust
impl TryFrom<&packed::ConnectionRequestDeliveredReader<'_>> for DeliverdContent {
    type Error = Status;

    fn try_from(value: &packed::ConnectionRequestDeliveredReader<'_>) -> Result<Self, Self::Error> {
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
            .map_err(|_| StatusCode::InvalidToPeerId.with_context("the to peer id is invalid"))?;
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L147-179)
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
            }
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L237-285)
```rust
    fn try_nat_traversal(&self, ttl: u64, remote_addrs: Vec<Multiaddr>) {
        let tasks = remote_addrs
            .into_iter()
            .filter_map(|listen_addr| match find_type(&listen_addr) {
                TransportType::Tcp => {
                    if listen_addr
                        .iter()
                        .any(|p| matches!(p, Protocol::Ip4(_) | Protocol::Ip6(_)))
                    {
                        Some(Box::pin(try_nat_traversal(self.bind_addr, listen_addr)))
                    } else {
                        None
                    }
                }
                TransportType::Memory
                | TransportType::Onion
                | TransportType::Ws
                | TransportType::Wss
                | TransportType::Tls => None,
            })
            .collect::<Vec<_>>();

        if tasks.is_empty() {
            return;
        }

        debug!("start NAT traversal");

        let control = self.p2p_control.clone();

        runtime::spawn(async move {
            runtime::delay_for(std::time::Duration::from_millis(ttl / 2)).await;
            if let Ok(((stream, addr), _)) = select_ok(tasks).await {
                debug!("NAT traversal success, addr: {:?}", addr);
                if let Some(metrics) = ckb_metrics::handle() {
                    metrics.ckb_hole_punching_active_success_count.inc();
                }
                let _ignore = control
                    .raw_session(
                        stream,
                        addr,
                        RawSessionInfo::outbound(TargetProtocol::Single(
                            SupportProtocols::Identify.protocol_id(),
                        )),
                    )
                    .await;
            }
        });
    }
```

**File:** network/src/protocols/hole_punching/mod.rs (L38-47)
```rust
pub(crate) struct HolePunching {
    network_state: Arc<NetworkState>,
    bind_addr: Option<SocketAddr>,
    // Request timestamp recorded
    inflight_requests: HashMap<PeerId, u64>,
    // Delivered timestamp recorded
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
}
```

**File:** network/src/protocols/hole_punching/mod.rs (L121-132)
```rust
            packed::HolePunchingMessageUnionReader::ConnectionRequestDelivered(reader) => {
                component::ConnectionRequestDeliveredProcess::new(
                    reader,
                    self,
                    context.control(),
                    context.session.id,
                    self.bind_addr,
                    msg.item_id(),
                )
                .execute()
                .await
            }
```
