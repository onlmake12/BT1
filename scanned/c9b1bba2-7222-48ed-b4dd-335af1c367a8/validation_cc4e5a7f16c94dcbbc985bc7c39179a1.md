Now I have all the code needed for a complete analysis. Let me trace the full exploit chain.

**Key code paths confirmed:**

1. `pending_delivered` is populated in `connection_request.rs` line 235-237 when the `to` node receives a `ConnectionRequest` — keyed by `from_peer_id`, storing the attacker-supplied listen addresses.

2. `ConnectionSyncProcess::execute()` in `connection_sync.rs` lines 98-99 unconditionally uses `content.route.last()` to forward — no validation that the route was established by a prior legitimate `ConnectionRequestDelivered`.

3. `forward_sync()` in `connection_sync.rs` lines 178-210 looks up any PeerId in the peer registry and sends the message — no origin check.

---

### Title
Unauthenticated `ConnectionSync` Route Injection Enables Arbitrary Peer Targeting and Remote NAT Traversal Task Spawning — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

### Summary
A relay node unconditionally forwards a `ConnectionSync` message to whichever peer ID appears in `route.last()`, with no validation that the route was established by a prior legitimate hole-punching flow. An attacker with a single P2P connection can (1) pre-populate `pending_delivered` at a target node via a crafted `ConnectionRequest`, then (2) send a crafted `ConnectionSync` to any relay connected to the target, causing the relay to forward the message to the target, which then spawns unbounded async NAT traversal tasks.

### Finding Description

**Step 1 — Pre-populate `pending_delivered` at the target:**

The attacker sends a `ConnectionRequest` with `from = attacker_peer_id`, `to = target_peer_id`, and attacker-controlled `listen_addrs`. When this reaches the target node (directly or via gossip relay), `ConnectionRequestProcess::respond_delivered` is called: [1](#0-0) 

This inserts `attacker_peer_id → (attacker_listen_addrs, now)` into `pending_delivered` with no authentication of the `from` identity.

**Step 2 — Inject a crafted `ConnectionSync` at a relay:**

The attacker sends a `ConnectionSync` message with `from = attacker_peer_id`, `to = target_peer_id`, `route = [target_peer_id]` to any relay node R that is connected to the target. The relay's `ConnectionSyncProcess::execute` checks only route length and a rate limiter keyed by the attacker-controlled `(from, to, msg_item_id)` triple: [2](#0-1) 

Since `route` is non-empty, it calls `forward_sync(target_peer_id)` with no check that R ever participated in a legitimate flow for this `(from, to)` pair.

**Step 3 — Relay forwards to the target:**

`forward_sync` resolves `target_peer_id` to a session ID via `peer_registry.get_key_by_peer_id` and sends the message: [3](#0-2) 

**Step 4 — Target spawns NAT traversal tasks:**

The target receives the forwarded `ConnectionSync` with an empty route. Since `self_peer_id == content.to` and `pending_delivered.get(&content.from)` returns the attacker's listen addresses (from Step 1), it spawns async NAT traversal tasks: [4](#0-3) 

Each task runs a 30-second TCP connection retry loop against attacker-controlled addresses. [5](#0-4) 

### Impact Explanation

- **Arbitrary peer targeting via relay**: Any relay can be weaponized to deliver `ConnectionSync` to any of its connected peers, violating the invariant that the relay must have participated in the original `ConnectionRequest` forwarding chain.
- **Remote async task spawning**: Each crafted `ConnectionSync` causes the target to spawn one async task per stored listen address (up to `ADDRS_COUNT_LIMIT = 24`), each running for up to 30 seconds with TCP socket creation and connection retries.
- **Rate limiter bypass**: The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)` — all attacker-controlled. By cycling through different `from` values, the attacker bypasses the 1-req/sec per-pair limit. The per-session limiter allows 30 messages/second per connection.
- **Amplification**: One attacker connection → relay forwards to N peers → each spawns up to 24 async tasks. With multiple connections or relays, this scales to significant resource exhaustion across the network.

### Likelihood Explanation

The attack requires only a standard P2P connection to any CKB relay node. No privileged access, no PoW, no leaked keys. The two-step setup (`ConnectionRequest` then `ConnectionSync`) is straightforward to implement. The `ConnectionRequest` gossip broadcast means Step 1 can be executed without a direct connection to the target.

### Recommendation

1. **Validate route provenance at the relay**: Before forwarding a `ConnectionSync`, the relay should verify it previously forwarded a `ConnectionRequestDelivered` for the same `(from, to)` pair along this route. Store a short-lived record of legitimate `(from, to)` pairs for which the relay acted as a relay in the `ConnectionRequestDelivered` phase.
2. **Authenticate `from` identity**: Require that the sender of `ConnectionSync` is the actual `from` peer (i.e., the session sending the message must match `content.from`).
3. **Decouple `pending_delivered` population from unauthenticated `ConnectionRequest`**: The `to` node should only store `pending_delivered` entries for `from` peers it has an existing or expected relationship with, or add a challenge-response step.

### Proof of Concept

```
1. Attacker A connects to relay R (standard P2P).
2. A sends ConnectionRequest{from=A_id, to=T_id, listen_addrs=[attacker_addr], max_hops=6, route=[]}
   via gossip; T receives it, calls respond_delivered, stores pending_delivered[A_id] = ([attacker_addr], now).
3. A sends ConnectionSync{from=A_id, to=T_id, route=[T_id]} to R.
4. R: route.last() = T_id → forward_sync(T_id) → peer_registry lookup → send_message_to(T_session).
5. T receives ConnectionSync{from=A_id, to=T_id, route=[]}.
   route.last() = None, self == to, pending_delivered[A_id] = Some([attacker_addr]).
   → runtime::spawn(select_ok([try_nat_traversal(attacker_addr)])) — 30-second TCP retry loop spawned.
6. Repeat step 3 with different A_id values to bypass rate limiter and spawn more tasks.
``` [6](#0-5) [7](#0-6)

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L161-167)
```rust
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L82-99)
```rust
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
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-163)
```rust
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
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L178-210)
```rust
    async fn forward_sync(&self, peer_id: &PeerId) -> Status {
        let target_sid = self
            .protocol
            .network_state
            .peer_registry
            .read()
            .get_key_by_peer_id(peer_id);

        match target_sid {
            Some(next_peer) => {
                let content = forward_sync(self.message);
                let new_message = packed::HolePunchingMessage::new_builder()
                    .set(content)
                    .build()
                    .as_bytes();
                let proto_id = SupportProtocols::HolePunching.protocol_id();
                debug!(
                    "forward the sync to next peer {} (id: {})",
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

**File:** network/src/protocols/hole_punching/component/mod.rs (L65-115)
```rust
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
