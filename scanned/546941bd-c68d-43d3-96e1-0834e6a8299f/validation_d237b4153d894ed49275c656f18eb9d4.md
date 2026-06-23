The code is fully readable. Let me trace the exact execution path to verify the claim.

### Title
Unauthenticated `from` Field in `ConnectionRequestDelivered` Allows Inflight-Request Drain and Attacker-Directed NAT Traversal — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

---

### Summary

Any connected peer can send a crafted `ConnectionRequestDelivered` message with `from` set to the victim node's own peer ID and `to` set to a peer ID present in `inflight_requests`. Because the `from` field is never authenticated against the actual sender's session identity, the node removes the legitimate in-flight entry and then initiates repeated outbound TCP connections to attacker-supplied addresses.

---

### Finding Description

`notify()` populates `inflight_requests` with `HashMap<PeerId, u64>` entries every `CHECK_INTERVAL` (5 minutes), keyed by the target peer ID and valued by the request timestamp. [1](#0-0) 

`execute()` reaches the sensitive branch when `content.route` is empty and `content.from == local_peer_id`:

```
match content.route.last() {
    None => {
        if self_peer_id != &content.from { ... forward ... }
        else {
            // ← attacker reaches here by setting from = local_peer_id
            let request_start = self.protocol.inflight_requests.remove(&content.to);
            ...
            self.try_nat_traversal(ttl, content.listen_addrs);
        }
    }
}
``` [2](#0-1) 

The `from` field is parsed directly from the wire message bytes with no check that it matches the actual session's peer ID: [3](#0-2) 

The `self.peer` (actual session ID) is only used in `respond_sync` to echo a message back; it is never compared against `content.from`. [4](#0-3) 

`try_nat_traversal` then spawns an async task that loops for up to 30 seconds, firing TCP `connect()` calls every ~200 ms to each attacker-supplied address (up to `ADDRS_COUNT_LIMIT = 24`): [5](#0-4) 

The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)`. Since the attacker controls both `from` and `to`, they can use distinct `to` values to bypass the limiter for each `inflight_requests` entry. [6](#0-5) 

The `listen_addrs` validation only checks that any embedded peer ID matches `content.to` — which the attacker also controls: [7](#0-6) 

---

### Impact Explanation

- **Inflight-request drain:** Every `inflight_requests` entry can be removed by a single crafted message, permanently suppressing legitimate hole-punching attempts until the next `notify()` cycle (5 minutes).
- **Attacker-directed TCP connections:** Up to 24 attacker-supplied addresses × ~150 TCP SYN attempts over 30 seconds = ~3,600 outbound SYN packets per attack invocation. This enables port-scan amplification against third-party hosts and connection-slot exhaustion on the victim node.
- **Raw session upgrade:** If any attacker-controlled endpoint accepts the TCP connection, `control.raw_session()` is called, potentially establishing a full P2P session to an attacker node under the guise of a legitimate hole-punch. [8](#0-7) 

---

### Likelihood Explanation

The attacker only needs a standard P2P connection to the victim. The `from` field requires no cryptographic proof. The attacker must know (or guess) a peer ID in `inflight_requests`, but these are broadcast via `ConnectionRequest` gossip to a square-root subset of peers, so a connected attacker can observe them directly. [9](#0-8) 

---

### Recommendation

In `execute()`, before entering the `inflight_requests.remove` branch, verify that the actual sender's peer ID (resolved from `self.peer` via the peer registry) equals `content.from`. Reject the message with a ban if they differ. This mirrors the existing pattern used elsewhere in the protocol where session identity is cross-checked against message fields.

---

### Proof of Concept

```
Pre-condition:
  victim.inflight_requests = { peer_B_id: T }   // populated by notify()

Attacker (connected as peer A) sends:
  ConnectionRequestDelivered {
    from:         victim_local_peer_id,   // spoofed
    to:           peer_B_id,              // known from gossip
    route:        [],                     // empty → triggers terminal branch
    listen_addrs: [/ip4/1.2.3.4/tcp/9999/p2p/<peer_B_id>],
    sync_route:   [],
  }

Execution in execute():
  content.route.last() == None            → enters None branch
  self_peer_id == content.from            → enters else branch (line 154)
  inflight_requests.remove(peer_B_id)     → Some(T), entry drained
  try_nat_traversal(ttl, [1.2.3.4:9999]) → spawns 30-second TCP connect loop

Assert:
  victim.inflight_requests.contains(peer_B_id) == false
  TCP SYN packets observed at 1.2.3.4:9999
```

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L223-235)
```rust
                    // Broadcast to a number of nodes equal to the square root of the total connection count using gossip.
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
```

**File:** network/src/protocols/hole_punching/mod.rs (L239-242)
```rust
            let now = unix_time_as_millis();
            for peer_id in inflight {
                self.inflight_requests.insert(peer_id, now);
            }
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L38-40)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L56-70)
```rust
                    Ok(mut addr) => {
                        if let Some(peer_id) = extract_peer_id(&addr) {
                            if peer_id != to {
                                return Err(StatusCode::InvalidListenAddrLen
                                    .with_context("peer id in listen address is invalid"));
                            }
                        } else {
                            addr.push(Protocol::P2P(Cow::Borrowed(to.as_bytes())));
                        }
                        Ok(addr)
                    }
                    Err(_) => Err(StatusCode::InvalidListenAddrLen
                        .with_context("the listen address is invalid")),
                },
            )
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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L215-235)
```rust
    async fn respond_sync(&self, from_peer_id: PeerId) -> Status {
        let content = init_sync(self.message);
        let new_message = packed::HolePunchingMessage::new_builder()
            .set(content)
            .build()
            .as_bytes();
        let proto_id = SupportProtocols::HolePunching.protocol_id();
        debug!(
            "current peer is the target peer {}, respond the sync back",
            from_peer_id
        );
        if let Err(error) = self
            .p2p_control
            .send_message_to(self.peer, proto_id, new_message)
            .await
        {
            StatusCode::ForwardError.with_context(error)
        } else {
            Status::ok()
        }
    }
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L265-284)
```rust
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
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L49-115)
```rust
pub(crate) async fn try_nat_traversal(
    bind_addr: Option<SocketAddr>,
    addr: Multiaddr,
) -> Result<(TcpStream, Multiaddr), std::io::Error> {
    let net_addr = match multiaddr_to_socketaddr(&addr) {
        Some(addr) => addr,
        None => {
            debug!("Failed to convert multiaddr to socketaddr");
            return Err(std::io::ErrorKind::InvalidInput.into());
        }
    };

    // Use a fixed interval but add a small amount of randomness
    let base_retry_interval = Duration::from_millis(200);

    // total time
    let timeout_duration = Duration::from_secs(30);
    let start_time = Instant::now();
    let mut retry_count = 0u32;
    while start_time.elapsed() < timeout_duration {
        retry_count += 1;

        // Add a small amount of random jitter (±25ms) to avoid conflicts
        // caused by continuous precise synchronization
        let jitter = Duration::from_millis(rand::random::<u64>() % 50);
        let actual_interval = if rand::random::<bool>() {
            base_retry_interval + jitter
        } else {
            base_retry_interval.saturating_sub(jitter)
        };

        let socket = create_socket(bind_addr, net_addr)?;

        match runtime::timeout(
            std::time::Duration::from_millis(200),
            socket.connect(net_addr),
        )
        .await
        {
            Ok(Ok(stream)) => {
                // try get the stored error in the underlying socket
                // if the socket is not connected, it will return an error
                if let Err(err) = check_connection(&stream) {
                    debug!("Failed to connect to NAT(base check): {}", err);
                }
                return Ok((stream, addr));
            }
            Err(err) => {
                debug!("Failed to connect to NAT(timeout): {}", err);
            }
            Ok(Err(err)) => {
                if err.kind() == std::io::ErrorKind::AddrNotAvailable {
                    return Err(err);
                }
                debug!(
                    "Failed to connect to NAT(other error): {}, {}",
                    err.kind(),
                    err
                );
            }
        }
        runtime::delay_for(actual_interval).await;
    }

    debug!("Failed to connect to NAT after {} retries", retry_count);
    Err(std::io::ErrorKind::TimedOut.into())
}
```
