The code is confirmed. Let me verify the exact behavior of `respond_sync` and the rate limiter key to complete the analysis.

Audit Report

## Title
Missing Sender-Identity Verification in `ConnectionRequestDeliveredProcess::execute` Allows Forged Delivery to Cancel Inflight Hole-Punch Requests and Redirect NAT Traversal to Attacker-Controlled Addresses — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

## Summary

`ConnectionRequestDeliveredProcess::execute` determines whether the local node is the originator of a hole-punch request by comparing `content.from` (an attacker-controlled field) against `self_peer_id`, without ever verifying that the actual P2P session delivering the message (`self.peer`) corresponds to `content.to`. Any connected peer can forge a `ConnectionRequestDelivered` with `from` set to the victim's own peer ID, `to` set to any peer ID currently in `inflight_requests`, an empty `route`, and `listen_addrs` pointing to attacker-controlled addresses. This causes the victim to silently remove the legitimate inflight entry and spawn outbound TCP connection attempts to attacker-controlled addresses.

## Finding Description

**Vulnerable branch in `execute()`:** [1](#0-0) 

When `content.route` is empty, the code checks only `self_peer_id != &content.from` at line 151. If the attacker sets `content.from` to the victim's own peer ID, this check passes and execution enters the `else` branch at line 154. There is no assertion that `self.peer` (the `PeerIndex` of the actual session that sent the message) equals the session ID corresponding to `content.to`. The `peer_registry` exposes `get_key_by_peer_id` for exactly this kind of lookup and is already used in `forward_delivered`: [2](#0-1) 

**Inflight entry removal and NAT traversal with attacker-controlled addresses:** [3](#0-2) 

`inflight_requests.remove(&content.to)` permanently removes the entry keyed by the attacker-supplied `content.to`, and `try_nat_traversal` is called with the attacker-supplied `content.listen_addrs`.

**`try_nat_traversal` establishes a raw P2P session on success:** [4](#0-3) 

On a successful TCP connection to an attacker-controlled address, `control.raw_session(stream, addr, RawSessionInfo::outbound(...))` is called, establishing a full P2P session running the Identify protocol with the attacker's server.

**How the attacker learns `inflight_requests` keys:**

`ConnectionRequest` messages are broadcast to a gossip subset of all connected peers: [5](#0-4) 

The `to` field of each broadcast is exactly the key inserted into `inflight_requests`. The attacker, being a connected peer, receives these broadcasts and trivially learns valid keys.

**Rate limiter does not prevent the attack:** [6](#0-5) 

The `forward_rate_limiter` is keyed by `(content.from, content.to, self.msg_item_id)`. Since `msg_item_id` is fixed per message type, this permits 1 forged delivery per second per `(from, to)` pair — sufficient to cancel any new inflight entry as soon as it is inserted.

**`respond_sync` sends `ConnectionSync` back to the attacker's session:** [7](#0-6) 

`send_message_to(self.peer, ...)` sends the sync response to the attacker's session, not to the legitimate `content.to` peer.

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker connected to many nodes can, at a cost of one message per second per target, continuously cancel their inflight hole-punch entries and force each victim to spawn repeated outbound TCP connection tasks toward attacker-controlled addresses. At scale this wastes connection-slot budget, generates spurious TCP traffic, and degrades the NAT traversal layer that nodes behind NAT depend on for peer connectivity. The `raw_session` call on success also allows the attacker to establish unauthorized P2P sessions that bypass normal peer-selection and connection policies.

## Likelihood Explanation

The attacker requires only a standard inbound or outbound P2P connection to the victim. The victim's peer ID is publicly advertised. Valid `inflight_requests` keys are directly observable from broadcast `ConnectionRequest` messages received over the same connection. No privileged access, key material, or hash power is required. The attack is repeatable at 1 Hz per `(from, to)` pair and is trivially parallelizable across many victims.

## Recommendation

Before entering the "local node is the originator" branch, verify that the actual session delivering the message (`self.peer`) corresponds to `content.to`. Use `peer_registry.get_key_by_peer_id(&content.to)` and assert the returned `SessionId` equals `self.peer`, mirroring the pattern already used in `forward_delivered`. If the check fails, return `StatusCode::Ignore` (or a ban-worthy status) rather than proceeding to remove the inflight entry or spawn NAT traversal tasks.

## Proof of Concept

```
1. Victim node V has peer ID P_V and has an inflight_requests entry for peer ID P_T
   (learned by attacker A from a broadcast ConnectionRequest).
2. A is connected to V with session S_A.
3. A sends a ConnectionRequestDelivered to V with:
     from         = P_V   (victim's own peer ID)
     to           = P_T   (key in inflight_requests)
     route        = []    (empty)
     sync_route   = []
     listen_addrs = [attacker_ip:attacker_port]
4. V's execute():
     content.route.last() == None  → enters the None branch
     self_peer_id == content.from  → enters the else branch (line 154)
     inflight_requests.remove(&P_T) → Some(start)  (entry consumed)
     respond_sync(P_V) → sends ConnectionSync back to S_A
     try_nat_traversal(ttl, [attacker_ip:attacker_port]) → spawned
5. V makes outbound TCP connections to attacker_ip:attacker_port.
   If attacker's server accepts, raw_session is called → unauthorized P2P session established.
   The legitimate hole-punch to P_T is permanently cancelled.
6. Repeat once per second to cancel any newly inserted inflight entry for P_T.
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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L182-188)
```rust
    async fn forward_delivered(&self, peer_id: &PeerId) -> Status {
        let target_sid = self
            .protocol
            .network_state
            .peer_registry
            .read()
            .get_key_by_peer_id(peer_id);
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L226-229)
```rust
        if let Err(error) = self
            .p2p_control
            .send_message_to(self.peer, proto_id, new_message)
            .await
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L267-283)
```rust
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
```

**File:** network/src/protocols/hole_punching/mod.rs (L224-241)
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
```
