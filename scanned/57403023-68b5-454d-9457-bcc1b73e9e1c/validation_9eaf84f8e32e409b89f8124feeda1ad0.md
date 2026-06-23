Looking at the full code path carefully before rendering a verdict.

### Title
Unbounded `runtime::spawn` Task Accumulation via Repeated `ConnectionSync` Messages — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

---

### Summary

An unprivileged remote peer can cause unbounded growth of async tasks in the CKB node's Tokio runtime by first seeding `pending_delivered` with attacker-controlled listen addresses via `ConnectionRequest`, then repeatedly sending `ConnectionSync` messages targeting the local node with different `from` peer IDs. Each accepted `ConnectionSync` unconditionally calls `runtime::spawn`, launching a task that runs `select_ok` over up to 24 `try_nat_traversal` futures, each of which loops for up to 30 seconds. No mechanism bounds the total number of concurrently live spawned tasks.

---

### Finding Description

**Step 1 — Seeding `pending_delivered`**

When the victim node receives a `ConnectionRequest` with `to = self_peer_id`, it calls `respond_delivered`, which inserts `(remote_listens, now)` into `pending_delivered` keyed by `from_peer_id`: [1](#0-0) 

There is no size cap on this `HashMap`. The attacker can populate it with arbitrarily many entries by using a different `from` peer ID in each `ConnectionRequest`. The outer `rate_limiter` (keyed `(session_id, item_id)`) allows 30 messages/second per session: [2](#0-1) 

The `forward_rate_limiter` (keyed `(from, to, msg_item_id)`) is bypassed by using a fresh `from` peer ID each time, since each new `from` is a distinct key. [3](#0-2) 

**Step 2 — Triggering unbounded spawns**

When the victim receives a `ConnectionSync` with `to = self_peer_id` and `from` present in `pending_delivered`, it unconditionally calls `runtime::spawn`: [4](#0-3) 

The spawned task runs `select_ok` over up to 24 `try_nat_traversal` futures. Each `try_nat_traversal` future loops retrying TCP connections for up to **30 seconds**: [5](#0-4) 

There is no semaphore, counter, or any other mechanism that bounds the total number of live spawned tasks.

**Why the rate limiter does not prevent this**

`msg_item_id` passed to `forward_rate_limiter` is the molecule union discriminant — a fixed constant (`2` for `ConnectionSync`), not an attacker-controlled field: [6](#0-5) 

So the `forward_rate_limiter` key for `ConnectionSync` is effectively `(from, to)`. By using a different `from` peer ID per message (all pre-seeded in `pending_delivered`), the attacker creates a distinct rate-limiter bucket for each message, bypassing the 1/second limit entirely. The outer `rate_limiter` at 30/second per session is the only real throttle.

**Steady-state resource consumption (single session)**

| Time elapsed | Spawned tasks alive | Concurrent `try_nat_traversal` futures |
|---|---|---|
| 30 s | 30 × 30 = 900 | 900 × 24 = 21,600 |
| 5 min (before cleanup) | 30 × 300 = 9,000 | 9,000 × 24 = 216,000 |

With multiple sessions the numbers multiply proportionally.

---

### Impact Explanation

Each `try_nat_traversal` future opens a `TcpSocket` and attempts a connection, consuming a file descriptor. 21,600+ concurrent futures exhaust the OS file descriptor limit (typically 1,024–65,536), causing all subsequent network I/O — including block relay, transaction propagation, and peer management — to fail with `EMFILE`/`ENFILE`. The Tokio runtime thread pool is also saturated by the polling overhead of hundreds of thousands of pending futures, causing the node to become unresponsive or crash.

---

### Likelihood Explanation

The attack requires only a single P2P connection to the victim. No privileged role, no PoW, no leaked key. The two-phase setup (seed `pending_delivered`, then send `ConnectionSync`) is straightforward to automate. The `pending_delivered` HashMap has no size limit and entries survive for 5 minutes (`TIMEOUT`): [7](#0-6) 

The cleanup interval is also 5 minutes (`CHECK_INTERVAL`), so in the worst case the attacker has a full 5-minute window to accumulate entries before any are pruned. [8](#0-7) 

---

### Recommendation

1. **Cap `pending_delivered` size**: Enforce a maximum number of entries (e.g., 64) and reject new `ConnectionRequest` messages when the cap is reached.
2. **Bound concurrent NAT traversal tasks**: Use a semaphore or a bounded task counter before calling `runtime::spawn` in `ConnectionSyncProcess::execute`. Reject or queue if the limit is exceeded.
3. **Deduplicate on `(from, to)`**: Before spawning, check whether a task for the same `(from, to)` pair is already live and skip if so.
4. **Tighten `pending_delivered` TTL**: Reduce `TIMEOUT` from 5 minutes to match `HOLE_PUNCHING_INTERVAL` (2 minutes) to shrink the attack window.

---

### Proof of Concept

```
1. Connect to victim node V (peer_id = V) as attacker peer A.
2. For i in 1..N:
     a. Generate a fresh fake peer ID F_i.
     b. Send ConnectionRequest { from=F_i, to=V, listen_addrs=[24 valid TCP addrs], max_hops=0, route=[] }
        → V calls respond_delivered, inserts pending_delivered[F_i] = ([24 addrs], now)
3. For i in 1..N (one per second, or all at once using different F_i to bypass forward_rate_limiter):
     a. Send ConnectionSync { from=F_i, to=V, route=[] }
        → V finds pending_delivered[F_i], spawns runtime::spawn with select_ok(24 try_nat_traversal futures)
4. After 30 seconds, assert via /proc/<pid>/fd or metrics that N*24 concurrent tasks are active.
   With N=900 (30/s × 30s), expect ~21,600 concurrent try_nat_traversal futures and fd exhaustion.
```

### Citations

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/mod.rs (L25-28)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
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

**File:** network/src/protocols/hole_punching/component/mod.rs (L65-114)
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

    debug!("Failed to connect to NAT after {} retries", retry_count);
    Err(std::io::ErrorKind::TimedOut.into())
```

**File:** util/gen-types/src/generated/protocols.rs (L5579-5584)
```rust
    pub fn item_id(&self) -> molecule::Number {
        match self {
            HolePunchingMessageUnionReader::ConnectionRequest(_) => 0,
            HolePunchingMessageUnionReader::ConnectionRequestDelivered(_) => 1,
            HolePunchingMessageUnionReader::ConnectionSync(_) => 2,
        }
```
