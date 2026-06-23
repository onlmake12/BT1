### Title
Self-Referential Hole Punching Messages Enable SSRF and Resource Exhaustion via Unauthenticated `from`/`to` Fields — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

An unprivileged connected peer can send a `ConnectionRequest` with `from = victim_peer_id` and `to = victim_peer_id` (both set to the victim's own `local_peer_id()`). Because there is no guard rejecting self-referential messages, `respond_delivered()` inserts `self_peer_id → [attacker_addresses]` into `pending_delivered`. A subsequent `ConnectionSync(from=self_peer_id, to=self_peer_id, route=[])` then causes the victim to call `try_nat_traversal()` against every attacker-controlled address, spawning long-lived background TCP tasks.

---

### Finding Description

**Step 1 — Poison `pending_delivered`**

In `ConnectionRequestProcess::execute()`, the only guard before calling `respond_delivered` is:

```rust
if self_peer_id == &content.to {
    self.respond_delivered(content.from, &content.to, content.listen_addrs).await
``` [1](#0-0) 

There is no check that `content.from != self_peer_id`. When the attacker sends `from = self_peer_id, to = self_peer_id`, the condition `self_peer_id == &content.to` is `true`, so `respond_delivered` is called with `from_peer_id = self_peer_id`.

Inside `respond_delivered`, after filtering attacker-supplied addresses to TCP/IPv4/IPv6 only, the function unconditionally inserts:

```rust
self.protocol.pending_delivered.insert(from_peer_id, (remote_listens, now));
``` [2](#0-1) 

This stores `self_peer_id → [attacker_ip:port, ...]` in `pending_delivered`.

**Step 2 — Trigger `try_nat_traversal` against attacker addresses**

In `ConnectionSyncProcess::execute()`, when `route` is empty and `self_peer_id == content.to`:

```rust
let listens_info = self.protocol.pending_delivered
    .get(&content.from)
    .map(|info| info.0.clone());
``` [3](#0-2) 

With `content.from = self_peer_id`, this retrieves the attacker-controlled addresses. Each address is then passed to `try_nat_traversal` in a spawned background task:

```rust
let tasks = listens.into_iter()
    .map(|listen_addr| Box::pin(try_nat_traversal(self.bind_addr, listen_addr)))
    .collect::<Vec<_>>();
// ...
runtime::spawn(async move { ... select_ok(tasks).await ... });
``` [4](#0-3) 

**`try_nat_traversal` behavior:** Each spawned task retries TCP connections to the target address for up to **30 seconds** with ~200ms intervals (~150 connection attempts per address). [5](#0-4) 

**Rate limiting does not prevent the attack:**

- The `forward_rate_limiter` is keyed by `(from, to, item_id)` at 1/second. With `from = to = self_peer_id`, the attacker can send 1 `ConnectionSync` per second.
- Each `ConnectionSync` spawns up to `ADDRS_COUNT_LIMIT = 24` background tasks.
- After 30 seconds: **30 × 24 = 720 concurrent background tasks**, each making TCP connections to attacker-controlled addresses. [6](#0-5) 

The `pending_delivered` interval check (2-minute cooldown per `from_peer_id`) only prevents re-poisoning, not re-triggering via `ConnectionSync`. [7](#0-6) 

---

### Impact Explanation

1. **SSRF-style outbound TCP connections**: The victim node initiates TCP connections to arbitrary attacker-controlled IP:port combinations. This can be used to probe internal network services not otherwise reachable from the attacker.
2. **Resource exhaustion**: Unbounded spawning of background tasks (up to 720 concurrent after 30 seconds, each holding a socket and retrying for 30 seconds) exhausts file descriptors, thread pool capacity, and CPU.

---

### Likelihood Explanation

Any peer connected to the victim over the P2P network can execute this attack. No special privileges, keys, or majority hashpower are required. The `from` and `to` fields in `ConnectionRequest` and `ConnectionSync` are unauthenticated byte fields — any peer can set them to any value, including the victim's own `local_peer_id()`. The attack is locally testable and requires only two crafted P2P messages.

---

### Recommendation

Add an explicit guard in `ConnectionRequestProcess::execute()` rejecting messages where `content.from == self_peer_id` or `content.from == content.to`:

```rust
if &content.from == self_peer_id || content.from == content.to {
    return StatusCode::Ignore.with_context("self-referential request rejected");
}
```

This should be placed before the `self_peer_id == &content.to` branch. Similarly, add a symmetric guard in `ConnectionSyncProcess::execute()` rejecting `content.from == self_peer_id` when the node is the `to` target. [8](#0-7) 

---

### Proof of Concept

```rust
// Pseudocode unit test
let victim_peer_id = node.local_peer_id().clone();

// Step 1: Send ConnectionRequest(from=victim, to=victim, listen_addrs=[attacker:1337])
let req = ConnectionRequest {
    from: victim_peer_id.clone(),
    to: victim_peer_id.clone(),
    listen_addrs: vec!["/ip4/1.2.3.4/tcp/1337".parse().unwrap()],
    route: vec![],
    max_hops: 6,
};
attacker_session.send(req);

// Assert: pending_delivered[victim_peer_id] == [/ip4/1.2.3.4/tcp/1337/p2p/<victim>]

// Step 2: Send ConnectionSync(from=victim, to=victim, route=[])
let sync = ConnectionSync {
    from: victim_peer_id.clone(),
    to: victim_peer_id.clone(),
    route: vec![],
};
attacker_session.send(sync);

// Assert: try_nat_traversal called with /ip4/1.2.3.4/tcp/1337
// Assert: TCP SYN packets observed toward 1.2.3.4:1337
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L127-153)
```rust
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-115)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L119-162)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L27-27)
```rust
const ADDRS_COUNT_LIMIT: usize = 24;
```
