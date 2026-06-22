Let me check the `pending_delivered` structure and `try_nat_traversal` function for full impact assessment.

The code confirms the vulnerability is real. Here is the complete analysis:

---

### Title
Unauthenticated `ConnectionSync` Bypasses Three-Way Handshake, Triggering NAT Traversal to Attacker-Controlled Addresses — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

### Summary

An unprivileged remote peer with a direct P2P connection to a victim node can force the victim to initiate outbound TCP connections (NAT traversal) to arbitrary attacker-controlled addresses by sending two sequential hole-punching protocol messages, completely bypassing the intended three-way handshake.

### Finding Description

The hole-punching protocol is designed as a three-phase handshake:
1. `ConnectionRequest` (A → relays → victim): victim stores `pending_delivered[A] = (A's addrs, now)` and sends `ConnectionRequestDelivered` back
2. `ConnectionRequestDelivered` (victim → relays → A): A receives it, sends `ConnectionSync`
3. `ConnectionSync` (A → relays → victim): victim reads `pending_delivered[A]`, initiates NAT traversal

The critical flaw is that `ConnectionSyncProcess::execute()` has **no guard** verifying that a `ConnectionRequestDelivered` round-trip actually occurred. It only checks whether `pending_delivered` contains an entry for `content.from` — a field entirely controlled by the message sender.

**Exact attack sequence:**

**Step 1** — Attacker sends `ConnectionRequest(from=A, to=victim, listen_addrs=[attacker_ip:port])` directly to victim.

In `connection_request.rs`, `execute()` checks `self_peer_id == &content.to` (line 145), which is true, so it calls `respond_delivered()`. That function stores the attacker-supplied addresses: [1](#0-0) 

**Step 2** — Attacker immediately sends `ConnectionSync(from=A, to=victim, route=[])` directly to victim.

In `connection_sync.rs`, `execute()` takes the `None` branch (empty route), confirms `self_peer_id == &content.to`, then reads `pending_delivered` using the attacker-controlled `content.from` key: [2](#0-1) 

Since `pending_delivered[A]` was populated in Step 1 with attacker-controlled addresses, `listens_info` is `Some(attacker_addrs)`. The victim then spawns `try_nat_traversal` to those addresses: [3](#0-2) 

If the TCP connection succeeds, the victim establishes a raw inbound P2P session with the attacker's server via `control.raw_session(stream, addr, RawSessionInfo::inbound(...))`.

There is no check anywhere in `ConnectionSyncProcess` that:
- The sender's actual session peer ID matches `content.from`
- A `ConnectionRequestDelivered` was ever received and processed for this `(from, to)` pair
- The `pending_delivered` entry was created through the legitimate relay path

The `pending_delivered` map is defined as `HashMap<PeerId, (Vec<Multiaddr>, u64)>` and is never cleared between the two message types: [4](#0-3) 

Entries only expire after `TIMEOUT = 5 minutes`, giving the attacker a wide window: [5](#0-4) 

### Impact Explanation

The victim node initiates outbound TCP connections to arbitrary attacker-controlled IP:port addresses. If the connection is accepted, the victim establishes a raw P2P session (`RawSessionInfo::inbound`) with the attacker's server, bypassing normal peer selection, connection limits, and peer scoring. This constitutes SSRF and unauthorized P2P session injection. The attacker can also use this to probe internal network addresses reachable from the victim.

### Likelihood Explanation

Any peer with a direct P2P connection to the victim can execute this in two messages. No special privileges, leaked keys, or majority hashpower are required. The `from` field is never cryptographically verified against the actual sender's session identity. The rate limiter (`forward_rate_limiter` keyed on `(from, to, msg_item_id)`) allows 1 request/second per pair, so the attack is repeatable. [6](#0-5) 

### Recommendation

In `ConnectionSyncProcess::execute()`, before reading `pending_delivered`, verify that the actual session peer ID of the sender matches `content.from`. Additionally, remove the `pending_delivered` entry after it is consumed by a `ConnectionSync` to prevent replay. The correct invariant is: a `ConnectionSync` should only trigger NAT traversal if the `from` peer ID matches the peer that sent the original `ConnectionRequest` **and** a `ConnectionRequestDelivered` was already sent back through the relay path.

### Proof of Concept

```
1. Attacker (peer A, direct connection to victim V) sends:
   ConnectionRequest { from: A, to: V, listen_addrs: [attacker_server:1234], route: [], max_hops: 6 }

2. Victim V processes ConnectionRequest:
   → self_peer_id == content.to → respond_delivered()
   → pending_delivered.insert(A, ([attacker_server:1234], now))
   → sends ConnectionRequestDelivered back to attacker (ignored)

3. Attacker immediately sends (without waiting for any relay):
   ConnectionSync { from: A, to: V, route: [] }

4. Victim V processes ConnectionSync:
   → route is empty, self_peer_id == content.to
   → pending_delivered.get(&A) → Some([attacker_server:1234])
   → spawns try_nat_traversal(bind_addr, attacker_server:1234)
   → victim initiates TCP connection to attacker_server:1234
   → if accepted: control.raw_session(stream, addr, RawSessionInfo::inbound(...))
   → unauthorized inbound P2P session established with attacker
``` [7](#0-6) [8](#0-7)

### Citations

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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L76-175)
```rust
    pub(crate) async fn execute(self) -> Status {
        let content = match SyncContent::try_from(&self.message) {
            Ok(content) => content,
            Err(status) => return status,
        };

        if content.route.len() > MAX_HOPS as usize {
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
                content.from, content.to, "ConnectionSync",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionSync");
        }

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

**File:** network/src/protocols/hole_punching/mod.rs (L44-44)
```rust
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
```

**File:** network/src/protocols/hole_punching/mod.rs (L173-174)
```rust
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/mod.rs (L255-257)
```rust
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```
