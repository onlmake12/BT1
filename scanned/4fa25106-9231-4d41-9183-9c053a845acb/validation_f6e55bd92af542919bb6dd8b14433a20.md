Audit Report

## Title
Unauthenticated `from=to=self` `ConnectionRequest` Poisons `pending_delivered`, Forcing Outbound TCP Connections to Attacker-Controlled Addresses — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary
The hole-punching protocol's `ConnectionRequestProcess::execute` has no guard requiring `content.from ≠ content.to` and no guard requiring `content.from` to match the actual sender's authenticated peer ID. An attacker who sets both `from` and `to` to the victim's own `PeerId` causes the victim to store attacker-supplied TCP addresses in `pending_delivered` keyed by its own peer ID. A subsequent `ConnectionSync` with `from = to = victim_peer_id` and empty route causes the victim to spawn `try_nat_traversal` tasks that retry TCP `connect()` to those attacker-controlled endpoints for 30 seconds, and on success establishes a full P2P session with the attacker's server.

## Finding Description

**Root cause — no `from ≠ to` and no `from` authentication:**

In `ConnectionRequestProcess::execute`, the only guards before the destination check are the route-loop check and the `forward_rate_limiter`: [1](#0-0) 

With an empty `route`, `content.route.contains(self_peer_id)` is false and execution continues. There is no check that `content.from ≠ content.to` and no check that `content.from` equals the actual sender's authenticated peer ID. The `forward_rate_limiter` is keyed by `(from, to, item_id)`: [2](#0-1) 

With `from = to = local_peer_id`, the key is `(local_peer_id, local_peer_id, item_id)` — 1 request/second, which is sufficient for a persistent attack.

**Step 1 — `ConnectionRequest` triggers `respond_delivered`:**

Since `to = local_peer_id`, the destination check passes and `respond_delivered` is called with `from_peer_id = local_peer_id`: [3](#0-2) 

**Step 2 — Attacker addresses stored in `pending_delivered`:**

Inside `respond_delivered`, the address validation only checks that any embedded P2P component matches `from`. Since `from = local_peer_id`, bare TCP addresses like `/ip4/1.2.3.4/tcp/9999` have `local_peer_id` appended automatically and pass: [4](#0-3) 

After filtering for TCP+IPv4/IPv6 (non-empty), the addresses are stored verbatim: [5](#0-4) 

The `HOLE_PUNCHING_INTERVAL` guard (2 minutes) only prevents re-poisoning the same key within 2 minutes — it does not prevent the initial poisoning.

**Step 3 — `ConnectionSync` triggers NAT traversal to attacker addresses:**

`ConnectionSyncProcess` does not receive the session ID at all (its constructor takes no peer ID parameter), so there is no mechanism to verify `content.from` against the sender. With empty `route`, `content.route.last()` is `None`: [6](#0-5) 

Since `self_peer_id == &content.to`, the else branch executes and looks up `pending_delivered[local_peer_id]`, finding the attacker's addresses. `try_nat_traversal` is spawned: [7](#0-6) 

`try_nat_traversal` retries TCP `connect()` to the attacker's socket for up to 30 seconds: [8](#0-7) 

On success, `control.raw_session(stream, addr, RawSessionInfo::inbound(listen_addr))` establishes a full P2P session with the attacker's server.

## Impact Explanation

This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** An attacker with a single connected peer slot can force the victim to initiate outbound TCP connections to arbitrary attacker-controlled endpoints for 30 seconds and, on success, establish an unauthorized full P2P session. The attacker can target many nodes simultaneously. The established session bypasses inbound firewall rules and allows the attacker to inject P2P protocol messages into the victim node.

## Likelihood Explanation

Any peer connected to the victim node can execute this with exactly two crafted messages. The victim's `PeerId` is publicly advertised on the P2P network. No special privileges, keys, or majority hashpower are required. The `forward_rate_limiter` allows 1 attempt/second for the `(local_peer_id, local_peer_id, item_id)` key, and the `HOLE_PUNCHING_INTERVAL` guard only prevents re-poisoning within 2 minutes — a single poisoning is sufficient to trigger the 30-second NAT traversal loop. The attack is repeatable every 2 minutes per victim node.

## Recommendation

1. **Reject `from == to`**: Add an explicit check in `ConnectionRequestProcess::execute` that returns an error if `content.from == content.to`.
2. **Authenticate `from`**: Verify that `content.from` matches the actual sender's authenticated peer ID (available from the session context as `context.session.id` mapped to its peer ID), so a peer cannot impersonate another peer ID in the `from` field.
3. **Reject `from == self`**: The node should reject any `ConnectionRequest` where `content.from == self_peer_id`, since a legitimate request would never originate from the node itself.
4. **Authenticate `from` in `ConnectionSync`**: Pass the sender's session ID to `ConnectionSyncProcess` and verify `content.from` matches the authenticated sender before looking up `pending_delivered`.

## Proof of Concept

```
1. Attacker connects to victim node as a normal P2P peer.
2. Attacker learns victim's PeerId (publicly advertised).
3. Attacker sends ConnectionRequest:
     from         = victim_peer_id
     to           = victim_peer_id
     listen_addrs = [/ip4/<attacker_ip>/tcp/<attacker_port>]
     route        = []
     max_hops     = 6
4. Victim: content.route is empty → route-loop check passes.
   Victim: self_peer_id == content.to → calls respond_delivered(victim_peer_id, victim_peer_id, [attacker_addr])
   Victim: no existing pending_delivered[victim_peer_id] → proceeds.
   Victim: /ip4/<attacker_ip>/tcp/<attacker_port> passes TCP+IPv4 filter → stored.
   → pending_delivered[victim_peer_id] = ([/ip4/<attacker_ip>/tcp/<attacker_port>], now)
5. Attacker sends ConnectionSync:
     from  = victim_peer_id
     to    = victim_peer_id
     route = []
6. Victim: content.route.last() is None → None branch.
   Victim: self_peer_id == content.to → else branch.
   Victim: pending_delivered[victim_peer_id] found → spawns try_nat_traversal([attacker_addr])
   → victim initiates TCP connect() to attacker_ip:attacker_port, retrying for 30 seconds.
   → on success: control.raw_session(stream, attacker_addr, RawSessionInfo::inbound(...))
     → full P2P session established with attacker's server.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L47-54)
```rust
                        if let Some(peer_id) = extract_peer_id(&addr) {
                            if peer_id != from {
                                return Err(StatusCode::InvalidListenAddrLen
                                    .with_context("peer id in listen address is invalid"));
                            }
                        } else {
                            addr.push(Protocol::P2P(Cow::Borrowed(from.as_bytes())));
                        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L127-130)
```rust
        let self_peer_id = self.protocol.network_state.local_peer_id();
        if content.route.contains(self_peer_id) {
            return StatusCode::Ignore.with_context("the message is passed, ignore it");
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L132-143)
```rust
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
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L98-115)
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
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L118-162)
```rust
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
