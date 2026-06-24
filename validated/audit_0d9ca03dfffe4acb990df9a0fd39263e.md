Audit Report

## Title
Unbounded `runtime::spawn` Accumulation via `ConnectionSync` Flood Causes File Descriptor Exhaustion — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

## Summary
A directly-connected peer can populate `pending_delivered` with a single `ConnectionRequest`, then flood `ConnectionSync` messages at the 1 req/sec rate permitted by `forward_rate_limiter`. Because each accepted `ConnectionSync` unconditionally calls `runtime::spawn` with up to 24 concurrent `try_nat_traversal` futures that each run for 30 seconds, tasks accumulate without bound. There is no global cap on concurrent spawned tasks or open sockets, making file descriptor exhaustion and node crash achievable by any connected peer.

## Finding Description

**Step 1 — Arm `pending_delivered` with a single `ConnectionRequest`.**

When the victim is the `to` target of a `ConnectionRequest`, `respond_delivered()` stores the attacker's listen addresses: [1](#0-0) 

The 2-minute `HOLE_PUNCHING_INTERVAL` guard only prevents re-insertion for the same `from` peer within 2 minutes: [2](#0-1) 

One insertion is sufficient. The entry persists for `TIMEOUT = 5 minutes`: [3](#0-2) 

**Step 2 — Trigger unbounded spawns via `ConnectionSync` flood.**

When `content.to == self_peer_id` and `pending_delivered` has a matching entry for `content.from`, the code unconditionally calls `runtime::spawn` with no task counter or guard: [4](#0-3) 

Each spawn passes all stored addresses to `select_ok`, creating up to 24 concurrent `try_nat_traversal` futures per spawn.

**Step 3 — Rate limiters do not prevent accumulation.**

The outer `rate_limiter` (30 req/sec per `(session_id, msg_item_id)`) and the inner `forward_rate_limiter` (1 req/sec per `(from, to, msg_item_id)`) are confirmed: [5](#0-4) 

The `forward_rate_limiter` is the binding constraint at 1 spawn/sec. Neither limiter tracks how many tasks are already running, so they cannot prevent accumulation.

**Step 4 — `try_nat_traversal` holds a socket per iteration for 30 seconds.**

Each `try_nat_traversal` call loops for 30 seconds, creating a new `TcpSocket` every ~200 ms: [6](#0-5) 

Accumulation math: `1 spawn/sec × 30 sec = 30 spawns × 24 tasks/spawn = 720 concurrent futures`, each cycling through sockets. At any moment, up to 720 sockets are open simultaneously. There is no global cap anywhere in the hole-punching protocol.

## Impact Explanation

File descriptor exhaustion on the victim node. With ~720 attacker-induced sockets plus normal node operation (P2P connections, database files, log handles), the process FD count approaches the Linux default per-process limit of 1024. Once exhausted, the node cannot accept new P2P connections, open database files, or perform other I/O, causing a node crash or severe degradation. This matches the allowed CKB bounty impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

The attacker only needs to be a connected peer — no special privilege, no PoW, no key material. The two-step setup (one `ConnectionRequest` then a 30-second `ConnectionSync` flood at 1/sec) is straightforward and deterministic. The attack is locally reproducible and repeatable after the 5-minute `TIMEOUT` window resets `pending_delivered`. The victim node must have at least one configured listen address (standard for any public node) for the spawn branch to execute.

## Recommendation

1. **Add a global concurrent-task counter** (e.g., `AtomicUsize`) for hole-punching NAT traversal tasks. Reject new spawns when the counter exceeds a documented maximum (e.g., 50), decrementing on task completion.
2. **Track and cancel in-flight tasks per `from` peer**: store a `JoinHandle` alongside the addresses in `pending_delivered` and abort the previous task before spawning a new one for the same `from`.
3. **Enforce a per-`from` spawn guard** so that if a `ConnectionSync` arrives for a `from` peer that already has a running task, the new spawn is rejected rather than added.

## Proof of Concept

```
1. Connect to victim as peer A (attacker).
2. Send ConnectionRequest{from=A, to=victim, listen_addrs=[24 × 192.0.2.1:1]}.
   → victim stores pending_delivered[A] = ([24 dead addrs], now)
3. Loop at 1/sec for 35 seconds:
     Send ConnectionSync{from=A, to=victim, route=[]}
   → each message passes forward_rate_limiter (1/sec) and spawns 24 try_nat_traversal tasks
4. After 30 seconds: 30 spawns × 24 tasks = 720 concurrent tasks open.
5. Monitor victim's /proc/<pid>/fd count — assert it approaches system ulimit.
6. Attempt to open a new legitimate P2P connection to victim — observe failure or
   monitor for EMFILE errors in victim logs.
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

**File:** network/src/protocols/hole_punching/mod.rs (L28-28)
```rust
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L143-163)
```rust
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

**File:** network/src/protocols/hole_punching/component/mod.rs (L65-84)
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
```
