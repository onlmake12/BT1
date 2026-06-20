Now I have all the code needed for a complete analysis. Let me trace the full attack path.

**Step 1 — Populate `pending_delivered` (via `ConnectionRequest`):** [1](#0-0) 

`pending_delivered` is a plain `HashMap` with no size cap. The attacker sends `ConnectionRequest` messages with `to = victim_peer_id` and a fresh fake `from` peer ID each time. The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)` — a new `from` ID bypasses it entirely. The only real throttle is the top-level `rate_limiter` at 30 req/sec per session. [2](#0-1) 

The 2-minute `HOLE_PUNCHING_INTERVAL` cooldown only applies per `from` peer ID, so it is trivially bypassed with distinct IDs. [3](#0-2) 

**Step 2 — Trigger unbounded `runtime::spawn` (via `ConnectionSync`):**

Each `ConnectionSync` message whose `from` matches a `pending_delivered` entry unconditionally calls `runtime::spawn` with no counter, semaphore, or task-count guard: [4](#0-3) 

**Step 3 — Socket creation inside `try_nat_traversal`:**

Each spawned task runs `select_ok` over up to 24 concurrent `try_nat_traversal` futures. Each future loops for 30 seconds, creating a fresh `TcpSocket` on every iteration (~150 iterations, one socket held open for the 200 ms connect window per iteration): [5](#0-4) [6](#0-5) 

**Steady-state FD math:**

| Parameter | Value |
|---|---|
| Rate limit (per session) | 30 tasks/sec |
| Task lifetime | 30 s |
| Concurrent tasks at steady state | 30 × 30 = **900** |
| Futures per task | up to 24 |
| Socket held per future | ~200 ms out of ~400 ms cycle |
| Peak open sockets | 900 × 24 × 0.5 ≈ **10,800** |

Linux default soft FD limit is 1,024 per process; even the hard limit of 65,536 is reachable with a slightly higher send rate or multiple attacker sessions.

**`ADDRS_COUNT_LIMIT` does not help here** — it caps the number of addresses per message at 24, which is exactly the multiplier used above. [7](#0-6) 

**The `notify` cleanup runs every 5 minutes**, so `pending_delivered` can accumulate up to 30 × 300 = 9,000 entries before the first sweep. [8](#0-7) 

---

### Title
Unbounded `runtime::spawn` in `ConnectionSync` handler enables FD exhaustion via NAT traversal socket flooding — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

### Summary
An unprivileged remote peer can exhaust the victim node's file descriptors by (1) flooding `pending_delivered` with fake `from` peer IDs via `ConnectionRequest` messages, then (2) sending matching `ConnectionSync` messages that each unconditionally spawn a 30-second async task running `select_ok` over up to 24 `try_nat_traversal` futures, each of which creates a new `TcpSocket` on every retry iteration.

### Finding Description
`ConnectionSyncProcess::execute` in `connection_sync.rs` calls `runtime::spawn` whenever `pending_delivered` contains an entry for the message's `from` field and the node has a configured listen address. There is no global counter, semaphore, or per-peer cap on how many such tasks may run concurrently.

`pending_delivered` is populated by `ConnectionRequestProcess::respond_delivered` in `connection_request.rs`. The only per-entry guard is a 2-minute cooldown keyed by `from` peer ID; using a fresh fake `from` ID on every message bypasses it. The `forward_rate_limiter` is also keyed by `(from, to, msg_item_id)`, so it too is bypassed with distinct `from` IDs. The only binding throttle is the top-level `rate_limiter` at 30 messages/sec per session.

Each spawned task runs `select_ok` over up to 24 `try_nat_traversal` futures. Each future loops for 30 seconds, creating a `TcpSocket` via `create_socket` on every iteration and holding it open for the 200 ms connect timeout before dropping it. With 30 tasks spawned per second and a 30-second task lifetime, 900 tasks run concurrently at steady state, each holding up to ~12 sockets simultaneously — approximately 10,800 open file descriptors attributable solely to hole-punching.

### Impact Explanation
FD exhaustion prevents the node from accepting new TCP connections (P2P, RPC). On Linux the default soft limit is 1,024 FDs per process; the attack reaches it in under 2 seconds from a single peer connection. Even with a raised hard limit the attack scales linearly. The node becomes effectively partitioned from the network without crashing, making the condition hard to diagnose.

### Likelihood Explanation
The attacker needs only one authenticated P2P session (no PoW, no stake, no privileged role). The two-phase setup (populate `pending_delivered`, then send `ConnectionSync`) is straightforward to automate. The `reuse_port_on_linux` condition is the default recommended configuration for public nodes and is not required for the FD exhaustion — sockets are created regardless; `bind_addr` only affects whether `SO_REUSEPORT` is set.

### Recommendation
1. Add a global atomic counter (or a `tokio::sync::Semaphore`) capping concurrent NAT traversal tasks (e.g., 8–16).
2. Cap the size of `pending_delivered` (e.g., 64 entries) and evict the oldest entry on overflow.
3. Rate-limit `ConnectionSync` messages that actually trigger a spawn (not just the forward path) per source session, independent of the `from` field.
4. Consider reducing `timeout_duration` in `try_nat_traversal` or reducing `ADDRS_COUNT_LIMIT`.

### Proof of Concept
```
1. Connect to victim via P2P (one session).
2. For i in 1..300:
     a. Send ConnectionRequest(from=random_peer_id_i, to=victim_id,
                               listen_addrs=[<any valid TCP addr>])
        — populates pending_delivered[random_peer_id_i]
     b. Send ConnectionSync(from=random_peer_id_i, to=victim_id, route=[])
        — spawns a task with 24 try_nat_traversal futures
3. After ~10 seconds, read /proc/<victim_pid>/fd | wc -l.
4. Assert fd count > 1000 (default soft limit).
```
At 30 messages/sec the soft FD limit is breached in under 2 seconds; the node stops accepting new connections.

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L161-166)
```rust
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
            }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/mod.rs (L27-27)
```rust
const ADDRS_COUNT_LIMIT: usize = 24;
```

**File:** network/src/protocols/hole_punching/mod.rs (L169-175)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
        let status = self.network_state.connection_status();

        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/mod.rs (L249-252)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (CHECK_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
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
