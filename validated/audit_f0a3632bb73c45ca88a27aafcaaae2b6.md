Audit Report

## Title
Unbounded `runtime::spawn` Task Accumulation via `ConnectionSync` with Distinct Fake `from` Peer IDs — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

## Summary
An unprivileged attacker with a single P2P session can exhaust OS file descriptors and crash a CKB node by first seeding `pending_delivered` with attacker-controlled entries via `ConnectionRequest` messages using distinct fake `from` peer IDs (bypassing the `forward_rate_limiter`), then sending `ConnectionSync` messages for each seeded entry. Each accepted `ConnectionSync` unconditionally calls `runtime::spawn`, launching a task that runs `select_ok` over up to 24 `try_nat_traversal` futures, each looping for 30 seconds and creating a new `TcpSocket` on every retry iteration. There is no bound on concurrent spawned tasks, no semaphore, and no deduplication guard.

## Finding Description

**Step 1 — Seeding `pending_delivered` without size cap**

`pending_delivered` is a plain `HashMap<PeerId, PendingDeliveredInfo>` with no maximum size: [1](#0-0) 

When V receives `ConnectionRequest { from=F_i, to=V }`, `respond_delivered` is called (line 145–147 of `connection_request.rs`), which inserts `(remote_listens, now)` keyed by `F_i`: [2](#0-1) 

The duplicate-entry guard only fires when the same `F_i` is reused: [3](#0-2) 

The `forward_rate_limiter` is keyed `(from, to, msg_item_id)`: [4](#0-3) 

With a fresh `F_i` per message, each gets its own rate-limiter bucket, bypassing the 1/second per-pair limit entirely. The only real throttle is the outer `rate_limiter` at 30 messages/second per `(session_id, item_id)`: [5](#0-4) 

**Step 2 — Triggering unbounded `runtime::spawn` calls**

When V receives `ConnectionSync { from=F_i, to=V, route=[] }`, the `forward_rate_limiter` is again keyed `(F_i, V, item_id)`: [6](#0-5) 

With distinct `F_i` values (all pre-seeded), each message gets its own bucket. The handler then looks up `pending_delivered[F_i]` and unconditionally calls `runtime::spawn` with no semaphore, counter, or deduplication guard: [7](#0-6) 

**Step 3 — Resource consumption inside each spawned task**

Each spawned task runs `select_ok` over up to 24 concurrent `try_nat_traversal` futures. Each future loops for up to 30 seconds, creating a new `TcpSocket` on every retry iteration (every ~200ms): [8](#0-7) 

During the 200ms connect window, each future holds an open socket FD. With 24 futures running concurrently per task, each task holds up to 24 sockets simultaneously.

**Why cleanup does not help**

`pending_delivered` is only pruned in the `notify` callback, which fires every 5 minutes (`CHECK_INTERVAL`), and entries survive for 5 minutes (`TIMEOUT`): [9](#0-8) [10](#0-9) 

The attacker has a full 5-minute window to accumulate entries and trigger spawns. Crucially, pruning `pending_delivered` does not cancel already-spawned tasks.

## Impact Explanation

At 30 `ConnectionSync` messages/second × 30 seconds = 900 live spawned tasks, each with up to 24 concurrent `try_nat_traversal` futures, the node sustains up to 21,600 concurrent futures each holding a `TcpSocket` FD during connect attempts. This exhausts the OS file descriptor limit (typically 1,024–65,536). All subsequent network I/O — block relay, transaction propagation, peer management — fails with `EMFILE`/`ENFILE`. The Tokio runtime thread pool is also saturated by polling overhead, causing the node to become unresponsive or crash. This matches the **High (10001–15000 points)** impact class: **"Vulnerabilities which could easily crash a CKB node."**

## Likelihood Explanation

The attack requires only a single P2P connection — no privileged role, no PoW, no leaked key. Generating distinct fake `PeerId` values is trivial (random bytes). The `forward_rate_limiter` bypass via distinct `from` peer IDs is reliable because the rate-limiter key space is unbounded. The two-phase setup is straightforward to automate and is repeatable and sustainable indefinitely from a single session.

## Recommendation

1. **Cap `pending_delivered` size**: Enforce a maximum number of entries (e.g., 64) and reject new `ConnectionRequest` messages when the cap is reached.
2. **Bound concurrent NAT traversal tasks**: Use a `tokio::sync::Semaphore` or an atomic counter before calling `runtime::spawn` in `ConnectionSyncProcess::execute`. Drop the message if the limit is exceeded.
3. **Deduplicate on `(from, to)`**: Before spawning, check whether a task for the same `(from, to)` pair is already live and skip if so.
4. **Tighten `pending_delivered` TTL**: Reduce `TIMEOUT` from 5 minutes to `HOLE_PUNCHING_INTERVAL` (2 minutes) to shrink the attack window.

## Proof of Concept

```
1. Connect to victim node V (peer_id = V) as attacker peer A.
2. For i in 1..N (up to 30/second via outer rate_limiter):
     a. Generate a fresh fake peer ID F_i.
     b. Send ConnectionRequest { from=F_i, to=V, listen_addrs=[1..24 valid TCP addrs], max_hops=0, route=[] }
        → V calls respond_delivered, inserts pending_delivered[F_i] = ([addrs], now)
        (forward_rate_limiter bypassed by distinct F_i; outer rate_limiter allows 30/sec)
3. For i in 1..N (up to 30/second):
     a. Send ConnectionSync { from=F_i, to=V, route=[] }
        → V finds pending_delivered[F_i], calls runtime::spawn(select_ok(24 try_nat_traversal futures))
        (forward_rate_limiter bypassed by distinct F_i)
4. After 30 seconds with N=900:
     - 900 live spawned tasks, each with up to 24 concurrent try_nat_traversal futures
     - Each future holds a TcpSocket FD during its 200ms connect window
     - Peak concurrent FDs: ~21,600
     - Assert FD exhaustion via /proc/<pid>/fd count or observe EMFILE errors in node logs
     - Node becomes unresponsive to block relay and peer management
```

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L25-28)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L44-44)
```rust
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
```

**File:** network/src/protocols/hole_punching/mod.rs (L95-107)
```rust
        if self
            .rate_limiter
            .check_key(&(session_id, msg.item_id()))
            .is_err()
        {
            debug!(
                "process {} from {}; result is {}",
                item_name,
                session_id,
                status::StatusCode::TooManyRequests.with_context(msg.item_name())
            );
            return;
        }
```

**File:** network/src/protocols/hole_punching/mod.rs (L172-175)
```rust
        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
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
