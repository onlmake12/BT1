All code claims verified against the actual source. Here is the validation result:

Audit Report

## Title
Unbounded `runtime::spawn` Task Accumulation via Repeated `ConnectionSync` Messages — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

## Summary
An unprivileged remote peer can exhaust the victim CKB node's file descriptors and Tokio runtime resources by seeding `pending_delivered` with attacker-controlled listen addresses via `ConnectionRequest` messages using distinct fake `from` peer IDs, then sending `ConnectionSync` messages for each seeded entry. Each accepted `ConnectionSync` unconditionally calls `runtime::spawn` with up to 24 concurrent `try_nat_traversal` futures, each looping for up to 30 seconds. No mechanism bounds the total number of live spawned tasks, enabling resource exhaustion that crashes the node.

## Finding Description

**Root cause — unbounded `pending_delivered` and no spawn guard**

`pending_delivered` is a plain `HashMap` with no capacity limit, initialized at construction with no bound. [1](#0-0) [2](#0-1) 

**Phase 1 — Seeding `pending_delivered`**

When `ConnectionRequest` arrives with `to == self_peer_id`, `respond_delivered` inserts `(remote_listens, now)` keyed by `from_peer_id` with no cap on total entries. [3](#0-2) 

The deduplication guard only blocks re-insertion of the *same* `from_peer_id` within `HOLE_PUNCHING_INTERVAL`. Using a fresh fake `from` peer ID per message bypasses this entirely, as `from_peer_id` is parsed directly from the message with no check that it corresponds to a connected peer. [4](#0-3) 

The `forward_rate_limiter` is keyed `(from, to, msg_item_id)`, so each new fake `from` is a distinct bucket and the 1/second limit is never triggered. [5](#0-4) 

The only real throttle is the outer `rate_limiter` at 30 messages/second per `(session_id, item_id)`, allowing 30 new `pending_delivered` entries per second from a single session. [6](#0-5) 

**Phase 2 — Triggering unbounded spawns**

When `ConnectionSync` arrives with `to == self_peer_id` and `content.from` present in `pending_delivered`, `runtime::spawn` is called unconditionally — no semaphore, no atomic counter, no deduplication check on whether a live task for the same `(from, to)` pair already exists. [7](#0-6) 

The same `forward_rate_limiter` bypass applies to `ConnectionSync` (keyed `(from, to, 2)`), so each distinct fake `from` gets its own bucket. [8](#0-7) 

Each spawned task runs `select_ok` over up to 24 `try_nat_traversal` futures. Each future loops retrying TCP connections for up to 30 seconds, allocating a new `TcpSocket` (file descriptor) on every ~200 ms iteration. [9](#0-8) 

**Why existing guards are insufficient**

- `forward_rate_limiter`: bypassed by using a distinct `from` peer ID per message.
- `pending_delivered` deduplication: only guards against the same `from` peer ID; different IDs are unconstrained.
- `TIMEOUT`/`CHECK_INTERVAL` cleanup: both set to 5 minutes, giving the attacker a full 5-minute accumulation window before any pruning. [10](#0-9) [11](#0-10) 

## Impact Explanation

Each `try_nat_traversal` iteration opens a `TcpSocket` (a file descriptor). At 30 spawns/second × 24 futures × ~150 iterations over 30 seconds, the node rapidly exhausts the OS file descriptor limit (typically 1,024–65,536), causing all subsequent network I/O — block relay, transaction propagation, peer management — to fail with `EMFILE`/`ENFILE`. The Tokio runtime thread pool is simultaneously saturated by polling hundreds of thousands of pending futures, causing the node to become unresponsive or crash.

This matches the **High (10001–15000 points)** impact: *"Vulnerabilities which could easily crash a CKB node."*

## Likelihood Explanation

The attack requires only a single authenticated P2P connection — no privileged role, no PoW, no leaked key. The two-phase setup (seed `pending_delivered`, then send `ConnectionSync`) is straightforward to automate. The attacker controls all relevant message fields (`from`, `to`, `listen_addrs`). The 30/second outer rate limit is the only real throttle, and it is sufficient to accumulate thousands of live tasks within the 5-minute cleanup window. The attack is repeatable and scalable across multiple sessions.

## Recommendation

1. **Cap `pending_delivered` size**: Enforce a maximum number of entries (e.g., 64) and reject new `ConnectionRequest` messages when the cap is reached.
2. **Bound concurrent NAT traversal tasks**: Use a `tokio::sync::Semaphore` or an atomic counter before calling `runtime::spawn` in `ConnectionSyncProcess::execute`. Reject or drop if the limit is exceeded.
3. **Deduplicate on `(from, to)`**: Before spawning, check whether a live task for the same `(from, to)` pair already exists and skip if so.
4. **Reduce `TIMEOUT`**: Shrink from 5 minutes to match `HOLE_PUNCHING_INTERVAL` (2 minutes) to reduce the accumulation window.

## Proof of Concept

```
1. Connect to victim node V (peer_id = V) as attacker peer A.
2. For i in 1..N (at 30/s, limited by outer rate_limiter):
     a. Generate a fresh fake peer ID F_i.
     b. Send ConnectionRequest { from=F_i, to=V, listen_addrs=[24 valid TCP addrs], max_hops=0, route=[] }
        → V calls respond_delivered, inserts pending_delivered[F_i] = ([24 addrs], now)
3. For i in 1..N (at 30/s, using distinct F_i to bypass forward_rate_limiter):
     a. Send ConnectionSync { from=F_i, to=V, route=[] }
        → V finds pending_delivered[F_i], calls runtime::spawn with select_ok(24 try_nat_traversal futures)
4. After 30 seconds (N = 900 at 30/s):
     - 900 spawned tasks alive, each with 24 try_nat_traversal futures = 21,600 concurrent futures
     - Each future opens a TcpSocket every ~200ms → rapid FD exhaustion
     - Assert via /proc/<pid>/fd count or node metrics that network I/O fails with EMFILE
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

**File:** network/src/protocols/hole_punching/mod.rs (L279-279)
```rust
            pending_delivered: HashMap::new(),
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
