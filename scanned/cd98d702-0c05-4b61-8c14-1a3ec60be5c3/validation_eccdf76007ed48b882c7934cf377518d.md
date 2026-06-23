### Title
Unverified Caller-Supplied `from` Peer ID in Hole-Punching `ConnectionRequest` Allows State Poisoning and Forced Outbound Connections - (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

The CKB hole-punching protocol parses the `from` peer ID directly from the message body without ever verifying it matches the actual sending peer. Any connected peer can forge a `ConnectionRequest` (and `ConnectionSync`) claiming to originate from an arbitrary victim peer ID. This poisons the target node's `pending_delivered` state with attacker-controlled listen addresses, and subsequently forces the target to initiate outbound TCP connections to attacker-chosen endpoints.

---

### Finding Description

**Root cause — unverified `from` field in `ConnectionRequest`**

`ConnectionRequestProcess::execute()` parses `content.from` entirely from the message payload: [1](#0-0) 

The only validation performed on `from` is that it is a syntactically valid `PeerId`. There is no check that `content.from` equals the peer ID of the actual session that delivered the message (`self.peer`). The actual sender's peer ID is available via the peer registry but is never consulted.

When the target node (`self_peer_id == &content.to`) processes the request, it calls `respond_delivered`, which stores the attacker-supplied listen addresses under the forged `from_peer_id` key: [2](#0-1) 

**Second stage — unverified `from` field in `ConnectionSync`**

`ConnectionSyncProcess` has the same defect: the `from` field is parsed from the message body and used to look up `pending_delivered` without verifying it matches the actual sender: [3](#0-2) 

**End-to-end exploit path**

1. Attacker (peer A, already connected) sends a `ConnectionRequest` with `from = victim_peer_B_id`, `to = target_peer_C_id`, `listen_addrs = [attacker_controlled_addr]`.
2. The message is forwarded through the relay chain to peer C.
3. Peer C's `respond_delivered` stores `pending_delivered[peer_B_id] = ([attacker_addr], now)` and sends `ConnectionRequestDelivered` back to peer A (the actual sender, `self.peer`).
4. Attacker sends a `ConnectionSync` with `from = peer_B_id`, `to = peer_C_id`.
5. Peer C receives `ConnectionSync`, looks up `pending_delivered[peer_B_id]`, retrieves `[attacker_addr]`, and calls `try_nat_traversal` — initiating an outbound TCP connection to the attacker's address.
6. On success, `control.raw_session(stream, addr, RawSessionInfo::inbound(listen_addr))` is called, establishing a full P2P session with the attacker's endpoint. [4](#0-3) 

The `forward_rate_limiter` key is `(content.from, content.to, msg_item_id)`. Because `content.from` is attacker-controlled, the rate limiter is keyed on the forged identity, not the real sender, so it provides no meaningful protection against this attack. [5](#0-4) 

The `inflight_requests` guard in `ConnectionRequestDelivered` only protects the *originating* node's own state; it does not prevent the target node from being poisoned via a forged `ConnectionRequest`. [6](#0-5) 

---

### Impact Explanation

**High.** A single connected peer with no special privileges can:

1. **Force arbitrary outbound TCP connections (SSRF):** The target node dials attacker-chosen IP:port combinations. This can be used to probe internal network services reachable from the victim node.
2. **Exhaust connection slots:** By repeatedly forging requests with distinct `from` IDs and different `msg_item_id` values (bypassing the rate limiter), the attacker can cause the target to open many outbound connections, exhausting file descriptors or connection pool limits.
3. **Poison `pending_delivered` state for legitimate peers:** If the attacker forges `from = real_peer_B`, peer C's stored listen addresses for peer B are replaced with attacker-controlled ones. Subsequent legitimate hole-punching between B and C will fail (DoS) or redirect C's NAT traversal to the attacker.
4. **Establish unsolicited P2P sessions:** A successful NAT traversal to the attacker's address results in `raw_session` being called, adding the attacker as a new inbound session — bypassing normal peer discovery and connection limits. [7](#0-6) 

---

### Likelihood Explanation

**High.** The attacker only needs to be a connected peer — a role reachable by any unprivileged external party. No keys, credentials, or privileged access are required. The hole-punching protocol is active whenever the node has outbound connections and needs NAT traversal. The forged `from` field requires no cryptographic material; it is a plain byte sequence accepted without signature verification. [8](#0-7) 

---

### Recommendation

Before processing a `ConnectionRequest` or `ConnectionSync`, verify that `content.from` matches the actual peer ID of the session that delivered the message. The actual peer ID can be resolved from the peer registry using `self.peer` (the `PeerIndex`):

```rust
// In ConnectionRequestProcess::execute(), after parsing content:
let actual_peer_id = self.protocol.network_state
    .peer_registry
    .read()
    .get_peer(self.peer)
    .map(|p| p.peer_id.clone());

if actual_peer_id.as_ref() != Some(&content.from) {
    return StatusCode::InvalidFromPeerId
        .with_context("from peer id does not match actual sender");
}
```

Apply the same check in `ConnectionSyncProcess`. This mirrors the fix recommended in the external report: use the verified caller identity (`msg.sender` / actual session peer ID) rather than a caller-supplied parameter. [9](#0-8) 

---

### Proof of Concept

```
Attacker (peer A) — already connected to relay node R and target node C

Step 1: Forge ConnectionRequest
  A → R → C:
    ConnectionRequest {
      from: <peer_B_id>,          // victim's peer ID, not A's
      to:   <peer_C_id>,
      listen_addrs: [<attacker_ip:port>],
      max_hops: 3,
      route: []
    }

Step 2: C processes request (connection_request.rs:146)
  C stores: pending_delivered[peer_B_id] = ([attacker_ip:port], now)
  C sends ConnectionRequestDelivered back to A (self.peer)

Step 3: Forge ConnectionSync
  A → R → C:
    ConnectionSync {
      from: <peer_B_id>,          // same forged identity
      to:   <peer_C_id>,
      route: [],
      sync_route: []
    }

Step 4: C processes sync (connection_sync.rs:111-115)
  listens_info = pending_delivered[peer_B_id] = [attacker_ip:port]
  C calls try_nat_traversal(ttl, [attacker_ip:port])
  → outbound TCP SYN to attacker_ip:port
  → on success: control.raw_session(...) establishes P2P session with attacker
``` [10](#0-9) [11](#0-10)

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L110-153)
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
    }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L155-239)
```rust
    async fn respond_delivered(
        &mut self,
        from_peer_id: PeerId,
        to_peer_id: &PeerId,
        remote_listens: Vec<Multiaddr>,
    ) -> Status {
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
            }
        }
        let listen_addrs = {
            let public_addr = self.protocol.network_state.public_addrs(ADDRS_COUNT_LIMIT);
            if public_addr.len() < ADDRS_COUNT_LIMIT {
                let observed_addrs = self
                    .protocol
                    .network_state
                    .observed_addrs(ADDRS_COUNT_LIMIT - public_addr.len());
                let iter = public_addr
                    .iter()
                    .chain(observed_addrs.iter())
                    .map(Multiaddr::to_vec)
                    .map(|v| packed::Address::new_builder().bytes(v).build());
                packed::AddressVec::new_builder().extend(iter).build()
            } else {
                let iter = public_addr
                    .iter()
                    .map(Multiaddr::to_vec)
                    .map(|v| packed::Address::new_builder().bytes(v).build());
                packed::AddressVec::new_builder().extend(iter).build()
            }
        };
        let content = init_delivered(self.message, listen_addrs);
        let new_message = packed::HolePunchingMessage::new_builder()
            .set(content)
            .build()
            .as_bytes();
        let proto_id = SupportProtocols::HolePunching.protocol_id();

        let remote_listens: Vec<Multiaddr> = remote_listens
            .into_iter()
            .filter_map(|addr| match find_type(&addr) {
                TransportType::Memory
                | TransportType::Onion
                | TransportType::Ws
                | TransportType::Wss
                | TransportType::Tls => None,
                TransportType::Tcp => {
                    if addr
                        .iter()
                        .any(|p| matches!(p, Protocol::Ip4(_) | Protocol::Ip6(_)))
                    {
                        Some(addr)
                    } else {
                        None
                    }
                }
            })
            .collect();

        if remote_listens.is_empty() {
            return StatusCode::Ignore.with_context("remote listen address is empty");
        }

        debug!(
            "current peer is the target peer {}, send a response back",
            to_peer_id
        );

        if let Err(error) = self
            .p2p_control
            .send_message_to(self.peer, proto_id, new_message)
            .await
        {
            return StatusCode::ForwardError.with_context(error);
        }

        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));

        Status::ok()
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L98-175)
```rust
        match content.route.last() {
            Some(next_peer_id) => self.forward_sync(next_peer_id).await,
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

                            if tasks.is_empty() {
                                return StatusCode::Ignore.with_context("no valid listen address");
                            }

                            debug!(
                                "current peer is the target peer {}, start NAT traversal",
                                content.to
                            );

                            match self
                                .protocol
                                .network_state
                                .config
                                .listen_addresses
                                .first()
                                .cloned()
                            {
                                Some(listen_addr) => {
                                    let control: ServiceAsyncControl = self.p2p_control.clone();
                                    runtime::spawn(async move {
                                        if let Ok(((stream, addr), _)) = select_ok(tasks).await {
                                            debug!("NAT traversal success, addr: {:?}", addr);
                                            if let Some(metrics) = ckb_metrics::handle() {
                                                metrics
                                                    .ckb_hole_punching_passive_success_count
                                                    .inc();
                                            }

                                            let _ignore = control
                                                .raw_session(
                                                    stream,
                                                    addr,
                                                    RawSessionInfo::inbound(listen_addr),
                                                )
                                                .await;
                                        }
                                    });
                                    Status::ok()
                                }
                                None => {
                                    StatusCode::Ignore.with_context("no listen address configured")
                                }
                            }
                        }
                        None => StatusCode::Ignore
                            .with_context("the from peer id is not in the pending list"),
                    }
                }
            }
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L160-176)
```rust
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
```

**File:** network/src/protocols/hole_punching/mod.rs (L72-120)
```rust
    async fn received(&mut self, context: ProtocolContextMutRef<'_>, data: bytes::Bytes) {
        let session_id = context.session.id;
        trace!("HolePunching.received session={}", session_id);

        let msg = match packed::HolePunchingMessageReader::from_slice(&data) {
            Ok(msg) => msg.to_enum(),
            _ => {
                warn!(
                    "HolePunching.received a malformed message from {}",
                    session_id
                );
                self.network_state.ban_session(
                    &context.control().clone().into(),
                    session_id,
                    BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };

        let item_name = msg.item_name();

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

        let status = match msg {
            packed::HolePunchingMessageUnionReader::ConnectionRequest(reader) => {
                component::ConnectionRequestProcess::new(
                    reader,
                    self,
                    context.session.id,
                    context.control(),
                    msg.item_id(),
                )
                .execute()
                .await
            }
```
