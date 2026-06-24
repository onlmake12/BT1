Audit Report

## Title
Unauthenticated `ConnectionRequestDelivered` Enables Unbounded NAT Traversal Toward Arbitrary IPs — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

## Summary
Any directly connected peer can craft a `ConnectionRequestDelivered` message with `from` set to the victim's own peer ID and an empty `route`, causing the terminal branch check (`self_peer_id == content.from`) to pass unconditionally. Combined with a valid `to` peer ID learned from the victim's own gossip broadcast, the attacker causes the victim to spawn async NAT traversal tasks that make repeated outbound TCP connections to arbitrary attacker-controlled IP addresses, turning the victim into a TCP SYN amplifier.

## Finding Description

**Root cause:** The terminal branch in `execute()` is gated only on `self_peer_id == content.from`, with no verification that the message actually traversed a legitimate relay chain. [1](#0-0) 

An attacker directly connected to the victim can set `content.from` to the victim's own peer ID (public, exchanged during identify handshake), satisfying this check without ever being a relay.

**Exploit flow:**

1. **`inflight_requests` population.** Every 5 minutes, `notify()` calls `fetch_nat_addrs()`, builds `ConnectionRequest` messages, gossip-broadcasts them to `sqrt(total)` peers, and inserts each `to_peer_id` into `inflight_requests` with a timestamp. [2](#0-1) 

2. **Attacker learns `to` peer IDs.** The attacker, as a connected peer, is among the `sqrt(total)` gossip recipients and receives the `ConnectionRequest` containing the exact `to_peer_id` stored in `inflight_requests`. [3](#0-2) 

3. **Attacker crafts the message.** The attacker sends a `ConnectionRequestDelivered` with `from` = victim's peer ID, `to` = observed peer ID, `route` = `[]`, and `listen_addrs` = up to 24 attacker-controlled TCP addresses.

4. **All validation gates pass.**
   - Valid molecule encoding → no ban.
   - `rate_limiter` (30 req/s per session): one message suffices.
   - `listen_addrs.len() <= ADDRS_COUNT_LIMIT (24)` and non-empty: attacker sends exactly 24.
   - `route.len() <= MAX_HOPS`: empty route passes.
   - `forward_rate_limiter` keyed `(from, to, item_id)` at 1/s: one message per distinct `to` suffices. [4](#0-3) 

5. **Terminal branch reached.** Empty `route` → `route.last()` is `None` → falls to the `else` branch. `self_peer_id == content.from` is true because the attacker set `from` = victim's peer ID. [1](#0-0) 

6. **`inflight_requests.remove(&content.to)` returns `Some(start)`.** The attacker used the observed peer ID, so the entry exists and is consumed. [5](#0-4) 

7. **`try_nat_traversal` spawns tasks.** The method filters the 24 addresses to TCP+IP ones, then calls `runtime::spawn` with a future that sleeps `ttl/2` ms and runs `select_ok(tasks)` — polling all 24 `try_nat_traversal` futures concurrently toward attacker-controlled IPs. [6](#0-5) 

8. **Each future retries for 30 seconds.** Each `try_nat_traversal` future creates a new `TcpSocket` every ~200ms for a hard 30-second timeout, generating ~150 outbound TCP SYN packets per address. [7](#0-6) 

**Why existing checks fail:**

- The `forward_rate_limiter` is keyed on `(from, to, item_id)`. Since the attacker uses a different `to` per inflight entry, each message is a distinct key and passes the 1/s limit. [8](#0-7) 

- The `listen_addrs` peer ID validation only checks that any embedded peer ID matches `content.to`, which the attacker controls — it does not restrict the IP addresses themselves. [9](#0-8) 

## Impact Explanation

Per exploited `inflight_requests` entry (K ≈ `max_outbound - non_whitelist_outbound` per 5-minute cycle):
- 1 spawned async task polling up to 24 concurrent TCP connection futures toward attacker-chosen IPs
- Each future: ~150 socket allocations over 30 seconds → ~3,600 outbound TCP SYN packets per address
- K entries per cycle → K × 3,600 SYN packets to arbitrary third-party hosts every 5 minutes, indefinitely

The victim is turned into a **TCP SYN amplifier** toward arbitrary hosts with no authentication required. This matches the **High** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* The attacker's cost is a single connected peer and one crafted message per cycle.

## Likelihood Explanation

- Requires only a standard P2P connection — no special privilege, no PoW, no key material
- Victim's peer ID is public (exchanged during identify handshake)
- Attacker has a non-trivial probability of receiving the `ConnectionRequest` gossip (probability ≈ `sqrt(total)/total`)
- `inflight_requests.remove()` limits each entry to one exploitation per 5-minute cycle, but the cycle repeats indefinitely
- No signature or nonce on `ConnectionRequestDelivered` verifies it traversed a legitimate relay path

## Recommendation

1. **Authenticate the delivery path:** Include a relay-chain signature or nonce in `ConnectionRequestDelivered` that the originating node can verify was produced by the actual relay chain, not injected directly by a connected peer.
2. **Verify sender session:** The terminal branch (`self_peer_id == content.from`) should only be reachable from a session known to be a legitimate relay (e.g., track which sessions forwarded the original `ConnectionRequest`).
3. **Cap concurrent NAT traversal tasks:** Maintain a global semaphore or task counter to bound the total number of simultaneously live NAT traversal tasks regardless of how many `inflight_requests` entries are consumed.
4. **Restrict `listen_addrs` to addresses associated with `content.to`:** Validate that IP addresses in `listen_addrs` are consistent with previously observed addresses for `content.to` in the peer store, preventing use of arbitrary third-party IPs.

## Proof of Concept

```rust
// 1. Connect attacker peer to victim node (standard P2P connection)
// 2. Wait for or observe ConnectionRequest gossip — extract `to_peer_id`
//    (attacker is among sqrt(total) gossip recipients)
// 3. Craft message:
let msg = ConnectionRequestDelivered::new_builder()
    .from(victim_peer_id_bytes)        // victim's own public peer ID (from identify)
    .to(observed_to_peer_id)           // from observed ConnectionRequest broadcast
    .route(BytesVec::default())        // empty → terminal branch, no forwarding
    .sync_route(BytesVec::default())
    .listen_addrs(build_24_tcp_addrs(attacker_target_ips))  // arbitrary third-party IPs
    .build();
// 4. Send to victim over HolePunching protocol session
// 5. Victim: route.last() == None, self_peer_id == content.from → terminal branch
//    inflight_requests.remove(&content.to) → Some(start)
//    try_nat_traversal spawned: 24 futures × ~150 TCP SYN packets to attacker_target_ips
// 6. Repeat for each observed to_peer_id in the same notify() cycle (K entries)
// Assert: K async tasks spawned, victim sends K×3600 SYN packets to attacker_target_ips
//         Attack repeats every 5 minutes indefinitely
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L57-64)
```rust
                        if let Some(peer_id) = extract_peer_id(&addr) {
                            if peer_id != to {
                                return Err(StatusCode::InvalidListenAddrLen
                                    .with_context("peer id in listen address is invalid"));
                            }
                        } else {
                            addr.push(Protocol::P2P(Cow::Borrowed(to.as_bytes())));
                        }
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L125-145)
```rust
        if content.listen_addrs.len() > ADDRS_COUNT_LIMIT || content.listen_addrs.is_empty() {
            return StatusCode::InvalidListenAddrLen
                .with_context("the listen address count is too large or empty");
        }

        if content.route.len() > MAX_HOPS as usize || content.sync_route.len() > MAX_HOPS as usize {
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
                content.from, content.to, "ConnectionRequestDelivered",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionRequestDelivered");
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L147-154)
```rust
        match content.route.last() {
            Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
            None => {
                let self_peer_id = self.protocol.network_state.local_peer_id();
                if self_peer_id != &content.from {
                    // forward the message to the `from` peer
                    self.forward_delivered(&content.from).await
                } else {
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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L237-284)
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
```

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

**File:** network/src/protocols/hole_punching/component/mod.rs (L65-111)
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
```
