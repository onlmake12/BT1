The code path is fully traceable. Let me confirm the exact conditions and flow before rendering a verdict.

**Attack path reconstruction:**

**Step 1 — Attacker gets their `PeerId` into `inflight_requests`**

`inflight_requests` is populated in `notify()` via `fetch_nat_addrs()` from the peer store. An attacker who is a connected peer can advertise themselves as a NAT-reachable peer through the Discovery protocol, causing the victim to insert the attacker's `PeerId` into `inflight_requests` on the next 5-minute tick. [1](#0-0) 

**Step 2 — Attacker crafts a `ConnectionRequestDelivered` message**

The attacker sends a message with:
- `from` = victim's own `PeerId` (public, announced via Identify)
- `to` = attacker's own `PeerId` (which is in `inflight_requests`)
- `route` = empty
- `listen_addrs` = attacker-controlled IP:port (with attacker's `PeerId` embedded or omitted)

**Step 3 — `listen_addrs` validation does NOT check IP ownership**

The only validation on `listen_addrs` is that if a `PeerId` is embedded in the address, it must match `content.to`. Since `content.to` is the attacker's own `PeerId`, the attacker can freely embed their own IP:port. [2](#0-1) 

**Step 4 — `execute()` reaches the NAT traversal branch**

With `route` empty and `self_peer_id == content.from`, the code removes the attacker's `PeerId` from `inflight_requests` and calls `try_nat_traversal` with the attacker-controlled addresses. [3](#0-2) 

**Step 5 — `try_nat_traversal` connects to attacker's TCP listener**

`try_nat_traversal` retries TCP connections for up to 30 seconds. When the attacker's listener accepts, `raw_session` is called with `RawSessionInfo::outbound(Identify)`. [4](#0-3) 

**Step 6 — `bind_addr` is NOT a required precondition**

The question states `bind_addr` must be set, but `try_nat_traversal` is called regardless. When `bind_addr` is `None`, `create_socket` simply creates an unbound socket that still connects to the attacker's endpoint. [5](#0-4) 

---

### Title
Unauthenticated Outbound Raw Session to Attacker-Controlled Endpoint via Crafted `ConnectionRequestDelivered` — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

### Summary
An unprivileged connected peer can craft a `ConnectionRequestDelivered` message with attacker-controlled `listen_addrs`, causing the victim node to establish an unauthenticated outbound raw session (running the Identify protocol) to an attacker-controlled TCP endpoint.

### Finding Description
`ConnectionRequestDeliveredProcess::execute()` reaches the NAT traversal branch when:
1. `content.route` is empty
2. `content.from` equals the local node's own `PeerId`
3. `content.to` matches a key in `inflight_requests`

All three conditions are attacker-controllable. The `from` field accepts any valid `PeerId` bytes — the victim's own `PeerId` is public. The `to` field can be the attacker's own `PeerId`, which the attacker can arrange to be in `inflight_requests` by advertising themselves as a NAT peer via Discovery. The `listen_addrs` validation only checks that any embedded `PeerId` matches `content.to`; it does not verify that the IP:port belongs to the claimed peer. [6](#0-5) 

Once the branch is reached, `try_nat_traversal` spawns a task that attempts TCP connections to the attacker-supplied addresses. On success, `p2p_control.raw_session(stream, addr, RawSessionInfo::outbound(Identify))` is called unconditionally. [7](#0-6) 

### Impact Explanation
The victim node establishes an outbound raw session to an attacker-controlled TCP endpoint, running the Identify protocol. The attacker controls the full Identify response, enabling peer-store poisoning with attacker-chosen addresses. This violates the invariant that outbound sessions are only established to peers discovered through authenticated peer-store channels. Downstream effects include potential eclipse attack setup and network topology manipulation.

### Likelihood Explanation
The attack requires only a standard P2P connection and the ability to register as a NAT peer in the victim's peer store (achievable via the Discovery protocol). The victim's `PeerId` is public. The `inflight_requests` window is 5 minutes, giving ample time to deliver the crafted message. No special privileges, leaked keys, or majority hashpower are required. The attack works on all platforms (not just Linux with `reuse_port_on_linux=true`).

### Recommendation
- Verify that the sender of a `ConnectionRequestDelivered` message is the peer whose `PeerId` matches `content.to` (i.e., validate that the message arrives from the session associated with `content.to`).
- Cross-check `listen_addrs` against the observed address of the connected peer that sent the message, rather than accepting arbitrary IP:port values.
- Consider binding `inflight_requests` entries to the specific session/peer that the `ConnectionRequest` was sent to, and reject `ConnectionRequestDelivered` messages that arrive from any other session.

### Proof of Concept
1. Attacker node `A` connects to victim `V` and advertises itself as a NAT peer via Discovery.
2. Wait for victim's `notify()` to fire (≤5 minutes); `A`'s `PeerId` is now in `V.inflight_requests`.
3. Attacker opens a TCP listener on `attacker_ip:attacker_port`.
4. Attacker sends a `HolePunchingMessage::ConnectionRequestDelivered` to `V` with:
   - `from` = `V`'s `PeerId` (obtained from Identify)
   - `to` = `A`'s `PeerId`
   - `route` = `[]`
   - `sync_route` = `[]`
   - `listen_addrs` = `[/ip4/attacker_ip/tcp/attacker_port/p2p/A_peer_id]`
5. `V` processes the message, removes `A` from `inflight_requests`, and calls `try_nat_traversal`.
6. `V` connects to `attacker_ip:attacker_port`; attacker's listener accepts.
7. `V` calls `raw_session(stream, attacker_addr, RawSessionInfo::outbound(Identify))`.
8. Assert: attacker's listener receives an Identify protocol handshake from `V`; attacker sends crafted Identify response poisoning `V`'s peer store.

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L179-242)
```rust
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
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L34-71)
```rust
impl TryFrom<&packed::ConnectionRequestDeliveredReader<'_>> for DeliverdContent {
    type Error = Status;

    fn try_from(value: &packed::ConnectionRequestDeliveredReader<'_>) -> Result<Self, Self::Error> {
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
            .map_err(|_| StatusCode::InvalidToPeerId.with_context("the to peer id is invalid"))?;
        let route = value
            .route()
            .iter()
            .map(|peer_id| {
                PeerId::from_bytes(peer_id.raw_data().to_vec())
                    .map_err(|_| StatusCode::InvalidRoute)
            })
            .collect::<Result<Vec<_>, _>>()?;
        let listen_addrs = value
            .listen_addrs()
            .iter()
            .map(
                |raw| match Multiaddr::try_from(raw.bytes().raw_data().to_vec()) {
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
            .collect::<Result<Vec<_>, _>>()?;
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

**File:** network/src/protocols/hole_punching/component/mod.rs (L117-148)
```rust
fn create_socket(
    bind_addr: Option<SocketAddr>,
    target_addr: SocketAddr,
) -> Result<TcpSocket, std::io::Error> {
    let socket = match bind_addr {
        Some(listen_addr) => match (listen_addr.ip(), target_addr.ip()) {
            (IpAddr::V4(_), IpAddr::V4(_)) => {
                let socket = TcpSocket::new_v4()?;
                socket.set_reuseaddr(true)?;
                #[cfg(all(unix, not(target_os = "solaris"), not(target_os = "illumos")))]
                socket.set_reuseport(true)?;
                socket.bind(listen_addr)?;
                socket
            }
            (IpAddr::V6(_), IpAddr::V6(_)) => {
                let socket = TcpSocket::new_v6()?;
                socket.set_reuseaddr(true)?;
                #[cfg(all(unix, not(target_os = "solaris"), not(target_os = "illumos")))]
                socket.set_reuseport(true)?;
                socket.bind(listen_addr)?;
                socket
            }
            (IpAddr::V4(_), IpAddr::V6(_)) => TcpSocket::new_v6()?,
            (IpAddr::V6(_), IpAddr::V4(_)) => TcpSocket::new_v4()?,
        },
        None => match target_addr.ip() {
            IpAddr::V4(_) => TcpSocket::new_v4()?,
            IpAddr::V6(_) => TcpSocket::new_v6()?,
        },
    };
    Ok(socket)
}
```
