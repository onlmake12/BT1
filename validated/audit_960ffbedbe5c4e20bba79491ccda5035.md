Audit Report

## Title
Unbounded `runtime::spawn` Accumulation via `ConnectionSync` Flood Causes File Descriptor Exhaustion — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

## Summary

An attacker who is a directly-connected peer can populate `pending_delivered` with a single `ConnectionRequest`, then flood `ConnectionSync` messages at the 1 req/sec rate permitted by `forward_rate_limiter`. Because each accepted `ConnectionSync` unconditionally calls `runtime::spawn` with up to 24 concurrent `try_nat_traversal` futures — each holding a TCP socket for up to 30 seconds — tasks accumulate without bound. Over the 30-second `try_nat_traversal` window, up to 720 concurrent sockets are held, risking file descriptor exhaustion and node crash.

## Finding Description

**Step 1 — Arm the attack via `ConnectionRequest`.**

The attacker (a directly-connected peer A) sends `ConnectionRequest{from=A, to=victim}` directly to the victim. Because `self_peer_id == content.to`, the victim calls `respond_delivered()`. [1](#0-0) 

Inside `respond_delivered()`, a `HOLE_PUNCHING_INTERVAL` (2-minute) guard prevents re-insertion for the same `from` within 2 minutes, but a single insertion is sufficient. [2](#0-1) 

After sending the response, the victim inserts `pending_delivered[A] = (attacker_listen_addrs, now)`. The entry persists for `TIMEOUT = 5 minutes`, giving the attacker a 5-minute exploitation window. [3](#0-2) [4](#0-3) 

**Step 2 — Trigger unbounded spawns via `ConnectionSync`.**

The attacker sends `ConnectionSync{from=A, to=victim}` at 1/sec. The `forward_rate_limiter` is the binding constraint, keyed by `(from, to, msg_item_id)` at 1 req/sec. [5](#0-4) [6](#0-5) 

Each message that passes the rate limiter reaches the `content.to == self_peer_id` branch, retrieves `pending_delivered[A]`, and unconditionally calls `runtime::spawn` with up to 24 concurrent `try_nat_traversal` futures passed to `select_ok`. There is no global cap on concurrent spawned tasks. [7](#0-6) 

**Step 3 — Socket accumulation math.**

Each `try_nat_traversal` future loops for 30 seconds, creating a new `TcpSocket` every ~200 ms. At any given moment, each task holds exactly 1 open file descriptor. [8](#0-7) 

```
1 spawn/sec × 30 sec accumulation window = 30 concurrent spawns
30 spawns × 24 tasks/spawn = 720 concurrent try_nat_traversal tasks
720 tasks × 1 FD each = ~720 concurrent open sockets
```

**Step 4 — Existing guards are insufficient.**

- The outer `rate_limiter` (30 req/sec per `(session_id, msg_item_id)`) is not the binding constraint.
- The `forward_rate_limiter` (1 req/sec per `(from, to, msg_item_id)`) slows but does not prevent accumulation because it has no memory of how many tasks are already running.
- The `HOLE_PUNCHING_INTERVAL` guard only prevents re-arming `pending_delivered`, not re-triggering spawns.
- There is no `JoinHandle` tracking, no per-`from` in-flight task counter, and no global concurrent-task cap anywhere in the hole-punching protocol. [9](#0-8) 

## Impact Explanation

**High — Vulnerability which could easily crash a CKB node.**

Linux default per-process FD limit is typically 1024. With ~720 FDs consumed by attacker-induced NAT traversal tasks, the victim node exhausts its FD budget. This prevents accepting new P2P connections, opening database files, and performing other I/O, effectively crashing or severely degrading the node. The impact is concrete and measurable via `/proc/<pid>/fd`.

## Likelihood Explanation

The attacker requires only a single direct P2P connection to the victim — no special privilege, no PoW, no key material. The two-step setup (one `ConnectionRequest` then a 30-second `ConnectionSync` flood at 1/sec) is trivial to implement and fully deterministic. The `pending_delivered` entry persists for 5 minutes, giving ample time to sustain the attack. The attack is locally reproducible.

## Recommendation

1. **Add a global concurrent-task counter** (e.g., `AtomicUsize`) for hole-punching NAT traversal tasks. Reject new spawns when the counter exceeds a documented maximum (e.g., 50).
2. **Track in-flight tasks per `from` peer**: store a `JoinHandle` alongside the addresses in `pending_delivered`. If a new `ConnectionSync` arrives for the same `from` while a task is still running, abort the old task before spawning a new one.
3. **Enforce a per-`from` spawn guard** in `execute()`: check whether a task for `content.from` is already running and return `StatusCode::Ignore` if so, rather than spawning unconditionally.

## Proof of Concept

```
1. Connect to victim as peer A (attacker).
2. Send ConnectionRequest{from=A, to=victim, listen_addrs=[24 × 192.0.2.1:1]}.
   → victim stores pending_delivered[A] = ([24 dead addrs], now)
3. Loop at 1/sec for 35 seconds:
     Send ConnectionSync{from=A, to=victim, route=[]}
   → each message passes forward_rate_limiter (1/sec) and spawns 24 try_nat_traversal tasks
4. After 30 seconds: 30 spawns × 24 tasks = 720 concurrent tasks, each holding 1 FD.
5. Monitor victim's /proc/<pid>/fd count — assert it approaches system ulimit (~1024).
6. Attempt to open a new legitimate P2P connection to victim — observe EMFILE failure.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
```

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

**File:** network/src/protocols/hole_punching/mod.rs (L28-28)
```rust
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L249-257)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (CHECK_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
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

**File:** network/src/protocols/hole_punching/component/mod.rs (L62-84)
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
```
