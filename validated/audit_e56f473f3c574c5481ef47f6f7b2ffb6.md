Audit Report

## Title
Unauthenticated `from` Field in `ConnectionRequestDelivered` Enables Inflight-Request Cancellation and Forced NAT Traversal to Attacker-Controlled Addresses — (File: `network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

## Summary
The `ConnectionRequestDelivered` handler parses `content.from` directly from the wire message with only syntactic validation and never cross-checks it against the authenticated sender's `PeerIndex`. Any connected peer can spoof `from = local_peer_id` with an empty `route`, enter the originator branch, permanently cancel legitimate inflight hole-punching requests, and force the victim node to make repeated TCP connection attempts to attacker-controlled addresses for up to 30 seconds per address.

## Finding Description
In `DeliverdContent::try_from`, `content.from` is parsed from the wire message using only `PeerId::from_bytes` — purely syntactic validation with no cross-check against the authenticated sender: [1](#0-0) 

The struct holds `self.peer: PeerIndex` (the authenticated session identity) but it is never used to validate `content.from`: [2](#0-1) 

In `execute()`, when `content.route` is empty, the code compares the local peer ID against the wire-supplied `content.from`: [3](#0-2) 

An attacker who sets `from = local_peer_id` (public via Identify) and `route = []` passes this check and enters the originator branch, where:
1. `inflight_requests.remove(&content.to)` permanently removes the inflight entry (line 160).
2. `try_nat_traversal(ttl, content.listen_addrs)` is called with attacker-supplied addresses (line 171).

The `forward_rate_limiter` is keyed on `(content.from, content.to, msg_item_id)`: [4](#0-3) 

Since `content.from` is the spoofed local peer ID and `content.to` is varied per inflight entry, the attacker bypasses per-key rate limiting with one message per inflight entry — exactly what is needed to cancel each entry.

The `try_nat_traversal` function in `component/mod.rs` makes TCP connection attempts with a 200ms timeout per attempt, retrying for 30 seconds total (~150 attempts per address), with up to `ADDRS_COUNT_LIMIT = 24` addresses run concurrently via `select_ok`: [5](#0-4) [6](#0-5) 

If the attacker supplies 24 addresses that never accept connections, all 24 async tasks run for 30 seconds each, generating ~3,600 total TCP connection attempts. Inflight entries are observable from gossiped `ConnectionRequest` broadcasts (sent to sqrt(N) peers every 5 minutes per `CHECK_INTERVAL`) and are repopulated on the same 5-minute cycle: [7](#0-6) [8](#0-7) 

## Impact Explanation
**High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

Persistent inflight-request cancellation prevents the victim node from completing NAT traversal and expanding its peer set. Repeated across multiple nodes by a single attacker (one message per inflight entry per 5-minute cycle, well within the session-level rate limit), this degrades network-wide connectivity by suppressing the hole-punching mechanism that allows NAT-ed nodes to join the peer graph. Additionally, forced TCP connections to attacker-controlled addresses constitute resource exhaustion (socket handles, async task memory, ~3,600 connection attempts per trigger) and enable port scanning of internal networks reachable from the victim.

## Likelihood Explanation
- **Entry path:** Any peer connected to the victim over the P2P network can send `HolePunchingMessage::ConnectionRequestDelivered`. No special role or key is required.
- **Required knowledge:** The local peer ID is public (Identify protocol). Inflight `to` peer IDs are observable from gossiped `ConnectionRequest` messages broadcast to sqrt(N) peers.
- **Rate limiting:** The session-level limiter keys on `(session_id, msg.item_id())` and the `forward_rate_limiter` keys on `(content.from, content.to, msg_item_id)`. The attacker needs only one message per inflight entry to cancel it; varying `content.to` across entries bypasses per-key limiting entirely.
- **Repeatability:** The attack repeats every 5-minute `notify` cycle as new inflight entries are populated.

## Recommendation
Before entering the originator branch, resolve the actual sender's `PeerId` from the authenticated session via `self.peer` (a `PeerIndex`) using the peer registry (`get_key_by_peer_id` / `get_peer`), which is already accessible via `self.protocol.network_state.peer_registry`. Compare the resolved peer ID against `content.from`; if they do not match, reject the message or treat it as a forwarding case rather than a terminal delivery. This ensures the originator branch is only reachable by the genuine originator. The `forward_delivered` function already demonstrates the correct pattern for registry lookup: [9](#0-8) 

## Proof of Concept
**Setup:** Attacker peer `A` is connected to victim `V`. `V` has recently broadcast a `ConnectionRequest` to peer `T` (observed by `A`), so `V.inflight_requests[T] = timestamp`.

**Step 1:** `A` crafts a `ConnectionRequestDelivered` molecule message:
- `from` = `V`'s peer ID (obtained from Identify handshake)
- `to` = `T`'s peer ID (observed from `V`'s gossiped `ConnectionRequest`)
- `route` = `[]`
- `sync_route` = `[]`
- `listen_addrs` = `[/ip4/192.168.1.1/tcp/9999/p2p/<T_peer_id>]` (attacker-controlled, up to 24 entries)

**Step 2:** `A` sends this message to `V` over the HolePunching protocol channel.

**Step 3:** `V` processes the message:
- `content.route.last()` → `None`
- `self_peer_id == &content.from` → **true** (spoofed)
- `inflight_requests.remove(&T)` → `Some(timestamp)` — **inflight request permanently cancelled**
- `try_nat_traversal(ttl, [/ip4/192.168.1.1/tcp/9999/...])` → **V makes ~150 TCP connection attempts to 192.168.1.1:9999 over 30 seconds**

**Verification:** A unit test can mock `network_state.local_peer_id()` and `inflight_requests`, send a crafted message with `from = local_peer_id` and `route = []`, and assert that (a) `inflight_requests` no longer contains the `to` entry and (b) `try_nat_traversal` is invoked with the attacker-supplied addresses.

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L38-40)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L92-98)
```rust
pub struct ConnectionRequestDeliveredProcess<'a> {
    message: packed::ConnectionRequestDeliveredReader<'a>,
    protocol: &'a mut HolePunching,
    p2p_control: &'a ServiceAsyncControl,
    peer: PeerIndex,
    bind_addr: Option<SocketAddr>,
    msg_item_id: u32,
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

**File:** network/src/protocols/hole_punching/mod.rs (L25-25)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
```

**File:** network/src/protocols/hole_punching/mod.rs (L27-27)
```rust
const ADDRS_COUNT_LIMIT: usize = 24;
```

**File:** network/src/protocols/hole_punching/mod.rs (L169-243)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
        let status = self.network_state.connection_status();

        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);

        if status.non_whitelist_outbound < status.max_outbound && status.total > 0 {
            let target = &self.network_state.required_flags;
            let addrs = self.network_state.with_peer_store_mut(|p| {
                p.fetch_nat_addrs(
                    (status.max_outbound - status.non_whitelist_outbound) as usize,
                    *target,
                )
            });

            let from_peer_id = self.network_state.local_peer_id();
            let listen_addrs = {
                let public_addr = self.network_state.public_addrs(ADDRS_COUNT_LIMIT);
                if public_addr.len() < ADDRS_COUNT_LIMIT {
                    let observed_addrs = self
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

            let mut inflight = Vec::new();
            for i in addrs {
                if let Some(to_peer_id) = extract_peer_id(&i.addr) {
                    let conn_req = {
                        let content = component::init_request(
                            from_peer_id,
                            &to_peer_id,
                            listen_addrs.clone(),
                        );
                        packed::HolePunchingMessage::new_builder()
                            .set(content)
                            .build()
                    };
                    let proto_id = SupportProtocols::HolePunching.protocol_id();

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
                }
            }

            let now = unix_time_as_millis();
            for peer_id in inflight {
                self.inflight_requests.insert(peer_id, now);
            }
        }
```
