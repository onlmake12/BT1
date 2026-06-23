### Title
Unbounded `runtime::spawn` Accumulation via `ConnectionSync` Flood Causes File Descriptor Exhaustion — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

### Summary

An unprivileged connected peer can cause the victim node to accumulate hundreds of concurrent `try_nat_traversal` tasks (each holding a TCP socket) by sending `ConnectionSync` messages at the rate permitted by `forward_rate_limiter`. There is no global cap on concurrent spawned tasks or open sockets, so over the 30-second `try_nat_traversal` timeout window, tasks accumulate without bound, risking file descriptor exhaustion.

---

### Finding Description

**Step 1 — Populate `pending_delivered`.**

The attacker (a directly-connected peer) sends a `ConnectionRequest` with `to = victim_peer_id` and up to 24 TCP addresses that never respond. In `connection_request.rs`, `respond_delivered()` stores the attacker's addresses: [1](#0-0) 

A `HOLE_PUNCHING_INTERVAL` (2-minute) guard prevents re-insertion for the same `from` peer ID within 2 minutes, but a single insertion is sufficient to arm the attack. [2](#0-1) 

**Step 2 — Trigger unbounded spawns via `ConnectionSync`.**

The attacker then sends `ConnectionSync` messages with `from = attacker_peer_id`, `to = victim_peer_id`. In `execute()`, when `content.to == self_peer_id` and `pending_delivered` has a matching entry, the code unconditionally calls `runtime::spawn`: [3](#0-2) 

Each spawn creates up to 24 concurrent `try_nat_traversal` futures (one per stored address) passed to `select_ok`. If the attacker's addresses never respond, none of the futures resolve early and all 24 run for the full 30-second timeout.

**Step 3 — Rate limiters do not prevent accumulation.**

Two rate limiters exist:

- `rate_limiter`: 30 req/sec per `(session_id, msg_item_id)` — outer check in `received()`
- `forward_rate_limiter`: **1 req/sec** per `(from, to, msg_item_id)` — inner check in `execute()` [4](#0-3) [5](#0-4) 

The `forward_rate_limiter` is the binding constraint at **1 spawn/sec** per `(from, to)` pair — not 30/sec as the question claims. However, because each `try_nat_traversal` task runs for **30 seconds**:

```
1 spawn/sec × 24 tasks/spawn × 30 sec accumulation = 720 concurrent tasks
```

**Step 4 — `try_nat_traversal` holds a socket per iteration.**

Each task loops for 30 seconds, creating a new TCP socket every ~200 ms: [6](#0-5) 

With 720 concurrent tasks each holding one socket at a time, the victim sustains ~720 concurrent open sockets. There is no global cap anywhere in the hole-punching protocol on the number of concurrent spawned tasks or sockets.

---

### Impact Explanation

File descriptor exhaustion on the victim node. Linux default per-process FD limit is typically 1024. With ~720 sockets consumed by attacker-induced NAT traversal tasks, the node's ability to accept new P2P connections, open database files, or perform other I/O degrades severely. Legitimate peers may be unable to connect.

---

### Likelihood Explanation

The attacker only needs to be a connected peer — no special privilege, no PoW, no key material. The two-step setup (`ConnectionRequest` then `ConnectionSync` flood) is straightforward. The `forward_rate_limiter` slows but does not prevent the accumulation because it has no memory of how many tasks are already running. The attack is locally testable and deterministic.

---

### Recommendation

1. **Add a global concurrent-task counter** (e.g., `AtomicUsize`) for hole-punching NAT traversal tasks. Reject new spawns when the counter exceeds a documented maximum (e.g., 50).
2. **Track and cancel in-flight tasks per `from` peer**: if a new `ConnectionSync` arrives for the same `from` while a task is still running, cancel the old task before spawning a new one.
3. **Enforce a per-`from` spawn guard** in `pending_delivered` (e.g., store a `JoinHandle` alongside the addresses and abort it on re-entry).

---

### Proof of Concept

```
1. Connect to victim as peer A (attacker).
2. Send ConnectionRequest{from=A, to=victim, listen_addrs=[24 × 0.0.0.0:1]}.
   → victim stores pending_delivered[A] = ([24 dead addrs], now)
3. Loop at 1/sec for 35 seconds:
     Send ConnectionSync{from=A, to=victim, route=[]}
   → each message passes forward_rate_limiter (1/sec) and spawns 24 try_nat_traversal tasks
4. After 30 seconds: 30 spawns × 24 tasks = 720 concurrent tasks open.
5. Monitor victim's /proc/<pid>/fd count — assert it approaches system ulimit.
6. Attempt to open a new legitimate P2P connection to victim — observe failure.
```

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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L85-96)
```rust
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
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L117-163)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L251-257)
```rust
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
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
