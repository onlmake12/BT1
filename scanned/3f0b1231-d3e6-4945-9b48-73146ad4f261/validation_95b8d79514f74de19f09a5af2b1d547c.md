I need to trace the full exploit path carefully through the code.

Now I have a complete picture. Let me trace the full exploit path precisely.

### Title
Missing `from != to` Validation Enables Resource Exhaustion via Self-Addressed Hole-Punching — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

An unprivileged connected peer can send a `ConnectionRequest` with `from == to == victim_node_peer_id`, causing the node to insert a `pending_delivered` entry keyed by its own peer ID. A subsequent `ConnectionSync` with the same self-referential IDs then triggers unbounded NAT traversal background tasks making hundreds of TCP connection attempts to attacker-controlled addresses. The rate limiter allows 1 such `ConnectionSync` per second; each spawns a 30-second task. After 30 seconds, 30 concurrent tasks × 24 concurrent TCP futures = **720 concurrent open sockets**, approaching typical file-descriptor limits and causing sustained resource exhaustion.

---

### Finding Description

**Missing guard:** There is no `from != to` check anywhere in the hole-punching message handlers. `grep` across all files under `network/src/protocols/hole_punching/` returns zero matches for any such validation.

**Step 1 — Poison `pending_delivered` via `ConnectionRequest` with `from == to == self_peer_id`:**

In `ConnectionRequestProcess::execute()`, the only peer-ID checks are:
- Line 128: `content.route.contains(self_peer_id)` — passes with an empty route
- Line 145: `self_peer_id == &content.to` — **TRUE** when `to == self_peer_id`, so `respond_delivered` is called with `from_peer_id = self_peer_id` [1](#0-0) 

Inside `respond_delivered`, attacker-supplied `listen_addrs` are filtered to TCP/IP-only addresses, then stored: [2](#0-1) 

Result: `pending_delivered[self_peer_id] = (attacker_tcp_addrs, now)`.

**Step 2 — Trigger NAT traversal via `ConnectionSync` with `from == to == self_peer_id`, empty route:**

In `ConnectionSyncProcess::execute()`:
- Empty route → `content.route.last()` is `None`
- Line 102: `self_peer_id != &content.to` → **FALSE** → enters the "current node is the `to` target" branch
- Line 114: `pending_delivered.get(&content.from)` where `content.from == self_peer_id` → **finds the poisoned entry**
- Lines 119–124: Creates `try_nat_traversal` futures for each stored address
- Lines 145–162: Spawns an async task running `select_ok(tasks)` — all 24 futures run concurrently for up to 30 seconds [3](#0-2) 

Critically, the `pending_delivered` entry is only `.get()`-read, never removed after use, so it persists for `TIMEOUT = 5 minutes`: [4](#0-3) 

**`try_nat_traversal` resource cost per task:**

Each invocation loops for up to 30 seconds, attempting a TCP connect every ~400ms (~75 attempts per address). With 24 addresses running concurrently via `select_ok`, each spawned task holds up to 24 open sockets simultaneously for 30 seconds. [5](#0-4) 

**Rate limiter analysis:**

`forward_rate_limiter` is keyed by `(from, to, msg_item_id)` at 1 req/sec. With `from == to == self_peer_id` and a fixed `msg_item_id` (union discriminant), the attacker is limited to 1 `ConnectionSync` per second. However, each invocation spawns a **30-second** background task. After 30 seconds: **30 concurrent tasks × 24 concurrent TCP futures = 720 concurrent open sockets**. [6](#0-5) 

---

### Impact Explanation

- **File descriptor exhaustion**: 720 concurrent TCP sockets approaches the typical non-root process fd limit (1024). Once exhausted, the node cannot accept new P2P connections, open database files, or perform any fd-requiring operation — effective DoS.
- **CPU/async runtime pressure**: 720 concurrent tokio tasks polling TCP futures continuously.
- **SSRF-like outbound connections**: The node makes TCP connections to arbitrary attacker-controlled IP:port combinations, enabling internal network port scanning or connection to internal services.
- **Sustained attack window**: The `pending_delivered` entry persists for 5 minutes, allowing the attacker to sustain the attack without re-sending the initial `ConnectionRequest`.

---

### Likelihood Explanation

The attacker only needs to be a connected P2P peer (no special privileges). The `HolePunching` protocol is enabled by default when `SupportProtocol::HolePunching` is in the config. [7](#0-6) 

The two-message sequence (`ConnectionRequest` then `ConnectionSync`) is straightforward to craft. The only precondition is supplying at least one valid TCP/IP multiaddr in `listen_addrs`, which is trivially satisfied.

---

### Recommendation

Add an explicit `from != to` guard at the top of `ConnectionRequestProcess::execute()` and `ConnectionSyncProcess::execute()`:

```rust
if content.from == content.to {
    return StatusCode::InvalidFromPeerId.with_context("from and to must be distinct peers");
}
```

Additionally, consider removing the `pending_delivered` entry after it is consumed in `ConnectionSyncProcess::execute()` (change `.get()` to `.remove()`) to prevent repeated triggering from a single poisoned entry.

---

### Proof of Concept

```
1. Connect to victim CKB node as a normal P2P peer (HolePunching protocol).

2. Obtain victim's peer ID (available via Identify protocol).

3. Send ConnectionRequest:
   - from  = victim_peer_id
   - to    = victim_peer_id   ← same as from
   - max_hops = 6
   - route = []               ← empty, bypasses route-loop check
   - listen_addrs = ["/ip4/192.168.1.1/tcp/8115/p2p/<victim_peer_id>"]
     (any valid TCP/IP address; attacker controls this target)

   Result: victim calls respond_delivered(victim_peer_id, ...),
   inserts pending_delivered[victim_peer_id] = ([192.168.1.1:8115], now),
   sends ConnectionRequestDelivered back to attacker.

4. Send ConnectionSync (once per second for 30+ seconds):
   - from  = victim_peer_id
   - to    = victim_peer_id
   - route = []

   Each message: victim finds pending_delivered[victim_peer_id],
   spawns async task with 24 concurrent try_nat_traversal futures,
   each looping for 30 seconds making TCP connects to 192.168.1.1:8115.

5. After 30 seconds: 30 concurrent background tasks × 24 TCP futures
   = 720 concurrent open sockets → file descriptor exhaustion → node DoS.
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-162)
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
```

**File:** network/src/protocols/hole_punching/mod.rs (L28-28)
```rust
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L44-46)
```rust
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L62-115)
```rust
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

**File:** network/src/network.rs (L940-953)
```rust
        // HolePunching protocol
        #[cfg(not(target_family = "wasm"))]
        if config
            .support_protocols
            .contains(&SupportProtocol::HolePunching)
        {
            let hole_punching_state = Arc::clone(&network_state);
            let hole_punching_meta =
                SupportProtocols::HolePunching.build_meta_with_service_handle(move || {
                    ProtocolHandle::Callback(Box::new(
                        crate::protocols::hole_punching::HolePunching::new(hole_punching_state),
                    ))
                });
            protocol_metas.push(hole_punching_meta);
```
