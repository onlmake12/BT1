Looking at the code carefully, I need to trace the exact execution path and identify whether the missing sender-identity check is exploitable.

The vulnerability is real. Here is the complete analysis:

---

### Title
Missing Sender-Identity Verification in `ConnectionRequestDelivered` Allows Forged Delivery to Cancel Inflight Hole-Punch Requests and Redirect NAT Traversal to Attacker-Controlled Addresses — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

### Summary

`ConnectionRequestDeliveredProcess::execute` determines whether the local node is the originator of a hole-punch request by comparing `content.from` (an attacker-controlled field in the message) against `self_peer_id`. It never verifies that the actual P2P session delivering the message (`self.peer`) corresponds to the expected `content.to` peer. An attacker who is already connected to the victim node can forge a `ConnectionRequestDelivered` with `from` set to the victim's own peer ID, `to` set to any peer ID currently in `inflight_requests`, an empty `route`, and `listen_addrs` pointing to attacker-controlled addresses. This causes the victim to: (1) silently remove the legitimate inflight entry, and (2) spawn outbound TCP connection attempts to attacker-controlled addresses, potentially establishing a raw P2P session with the attacker.

### Finding Description

**Vulnerable branch in `execute()`:** [1](#0-0) 

When `content.route` is empty, the code checks only whether `self_peer_id == content.from`. If true, it treats the local node as the originator and proceeds to consume `inflight_requests`: [2](#0-1) 

There is no check that `self.peer` (the actual session that sent the message) is the session corresponding to `content.to`. The `peer_registry` has `get_key_by_peer_id` available and is used elsewhere in the same file for exactly this kind of lookup: [3](#0-2) 

**How the attacker learns `inflight_requests` keys:**

The victim node broadcasts `ConnectionRequest` messages to a gossip subset of connected peers, including the attacker: [4](#0-3) 

The `to` field of each broadcast `ConnectionRequest` is exactly the key inserted into `inflight_requests`. The attacker, being a connected peer, receives these broadcasts and trivially learns valid keys.

**NAT traversal to attacker-controlled addresses:** [5](#0-4) 

`try_nat_traversal` makes repeated outbound TCP connection attempts for up to 30 seconds. On success it calls `control.raw_session(stream, addr, RawSessionInfo::outbound(...))`, establishing a raw P2P session running the Identify protocol with the attacker's server.

**Rate limiter does not prevent the attack:**

The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)`: [6](#0-5) 

This allows 1 forged delivery per second per `(from, to)` pair — sufficient to cancel any new inflight entry as soon as it is inserted.

### Impact Explanation

1. **Outbound TCP connections to attacker-controlled addresses**: The victim makes repeated TCP connection attempts to addresses chosen by the attacker. If the attacker's server accepts, `raw_session` is called, potentially establishing a full P2P session with the attacker's node, bypassing normal peer selection and connection policies.
2. **Silent cancellation of legitimate hole-punch attempts**: The `inflight_requests` entry for the targeted peer is permanently removed, preventing the legitimate NAT traversal from completing.

### Likelihood Explanation

The attacker only needs a standard inbound or outbound P2P connection to the victim. The `to` peer IDs in `inflight_requests` are directly observable from broadcast `ConnectionRequest` messages. The victim's own peer ID (`self_peer_id`) is publicly advertised. No privileged access, key material, or hash power is required.

### Recommendation

Before entering the "local node is the originator" branch, verify that the actual session delivering the message (`self.peer`) corresponds to `content.to` (or to the last relay node in `sync_route`). Use `peer_registry.get_key_by_peer_id(&content.to)` and assert it equals `self.peer`, mirroring the pattern already used in `forward_delivered`.

### Proof of Concept

```
1. Victim node V has peer ID P_V and has an inflight_requests entry for peer ID P_T
   (learned by attacker A from a broadcast ConnectionRequest).
2. A is connected to V with session S_A.
3. A sends a ConnectionRequestDelivered to V with:
     from  = P_V   (victim's own peer ID)
     to    = P_T   (key in inflight_requests)
     route = []    (empty)
     listen_addrs = [attacker_ip:attacker_port]
4. V's execute():
     content.route.last() == None  → enters the None branch
     self_peer_id == content.from  → enters the else branch (line 154)
     inflight_requests.remove(&P_T) → Some(start)  (entry consumed)
     respond_sync(P_V) → sends ConnectionSync back to S_A
     try_nat_traversal(ttl, [attacker_ip:attacker_port]) → spawned
5. V makes outbound TCP connections to attacker_ip:attacker_port.
   If attacker's server accepts, raw_session is called → P2P session established.
   The legitimate hole-punch to P_T is permanently cancelled.
```

### Citations

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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L182-213)
```rust
    async fn forward_delivered(&self, peer_id: &PeerId) -> Status {
        let target_sid = self
            .protocol
            .network_state
            .peer_registry
            .read()
            .get_key_by_peer_id(peer_id);
        match target_sid {
            Some(next_peer) => {
                let content = forward_delivered(self.message);
                let new_message = packed::HolePunchingMessage::new_builder()
                    .set(content)
                    .build()
                    .as_bytes();
                let proto_id = SupportProtocols::HolePunching.protocol_id();
                debug!(
                    "forward the delivery to next peer {} (id: {})",
                    next_peer, peer_id
                );
                if let Err(error) = self
                    .p2p_control
                    .send_message_to(next_peer, proto_id, new_message)
                    .await
                {
                    StatusCode::ForwardError.with_context(error)
                } else {
                    Status::ok()
                }
            }
            None => StatusCode::Ignore.with_context("the next peer in the route is disconnected"),
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

**File:** network/src/protocols/hole_punching/mod.rs (L224-242)
```rust
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
                }
            }

            let now = unix_time_as_millis();
            for peer_id in inflight {
                self.inflight_requests.insert(peer_id, now);
            }
```
