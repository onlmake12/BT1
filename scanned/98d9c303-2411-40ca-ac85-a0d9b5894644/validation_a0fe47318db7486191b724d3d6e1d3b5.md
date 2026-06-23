Now I have a complete picture of the code. Let me analyze the full attack path.

**Key findings:**

1. **`pending_delivered` population**: An attacker can populate `pending_delivered[attacker_peer_id]` by sending a single `ConnectionRequest` with `to = local_peer_id` and valid TCP listen addresses. The `HOLE_PUNCHING_INTERVAL` check only prevents re-insertion within 2 minutes, but the entry persists for `TIMEOUT = 5 minutes`.

2. **Rate limiter bypass**: Both rate limiters are keyed with `item_id` as part of the key. The `forward_rate_limiter` is keyed by `(from, to, item_id)` at 1/second — with N distinct `item_id`s, N distinct buckets are created, allowing N messages/second to pass.

3. **Unbounded `runtime::spawn`**: Each passing `ConnectionSync` message with `self_peer_id == content.to` and a valid `pending_delivered` entry unconditionally calls `runtime::spawn`, with no cap on concurrent tasks.

4. **Each task**: Runs `select_ok(tasks)` over up to `ADDRS_COUNT_LIMIT = 24` concurrent `try_nat_traversal` futures, each retrying TCP connections for up to 30 seconds.

---

### Title
Unbounded `runtime::spawn` of NAT Traversal Tasks via `ConnectionSync` with Distinct `item_id`s — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

### Summary
An unprivileged connected peer can exhaust the async task pool, CPU, and socket file descriptors by sending a stream of `ConnectionSync` messages targeting the local node, each with a distinct `item_id`, after first seeding one entry in `pending_delivered`. The rate limiters are keyed on `item_id`, so using distinct values creates independent buckets and bypasses the intended per-session throttle. Each accepted message unconditionally spawns an async task that opens up to 24 TCP sockets and retries for 30 seconds.

### Finding Description

**Step 1 — Seed `pending_delivered`**

The attacker sends one `ConnectionRequest` with `from = attacker_peer_id`, `to = local_peer_id`, and valid TCP listen addresses. In `connection_request.rs` `respond_delivered()`, the local node inserts the attacker's peer ID into `pending_delivered`: [1](#0-0) 

The entry persists for `TIMEOUT = 5 minutes`. [2](#0-1) 

**Step 2 — Bypass rate limiters with distinct `item_id`s**

The outer rate limiter in `received()` is keyed by `(session_id, msg.item_id())` at 30/second: [3](#0-2) 

The `forward_rate_limiter` in `ConnectionSyncProcess::execute()` is keyed by `(from, to, item_id)` at 1/second: [4](#0-3) 

Since `item_id` is part of both keys, each distinct `item_id` creates an independent rate-limit bucket. With N distinct `item_id`s (up to 2³² values), the attacker passes N messages/second through both limiters.

**Step 3 — Unconditional `runtime::spawn` per message**

Once both rate limiters pass and `pending_delivered` contains the attacker's `from` peer ID, `execute()` unconditionally calls `runtime::spawn` with no cap on concurrent tasks: [5](#0-4) 

**Step 4 — Each task opens up to 24 TCP sockets for 30 seconds**

`try_nat_traversal` retries TCP connections in a loop for up to 30 seconds: [6](#0-5) 

`ADDRS_COUNT_LIMIT = 24` addresses are passed as tasks to `select_ok`: [7](#0-6) 

### Impact Explanation

In a 30-second window with N=1,000 distinct `item_id`s: 30,000 async tasks are spawned, each holding up to 24 open TCP sockets = up to 720,000 socket descriptors. This exhausts the OS file descriptor limit (typically 65,535 on Linux), the Tokio thread/task pool, and CPU from continuous TCP retry loops — causing the node to become unresponsive or crash.

### Likelihood Explanation

The attacker only needs a single P2P connection to the victim node (standard unprivileged peer). The `ConnectionRequest` to seed `pending_delivered` is a normal protocol message. Sending thousands of `ConnectionSync` messages with distinct `item_id`s requires no special privilege, no PoW, and no cryptographic material beyond a valid peer identity.

### Recommendation

1. **Bound concurrent tasks per `(from, to)` pair**: Track active NAT traversal tasks per session and reject new spawns if a task is already running for that pair.
2. **Fix the rate limiter key**: Key the `forward_rate_limiter` by `(from, to)` only, dropping `item_id` from the key, so the 1/second limit applies to the entire `(from, to)` pair regardless of `item_id`.
3. **Global task semaphore**: Introduce a bounded semaphore (e.g., `tokio::sync::Semaphore`) to cap the total number of concurrent NAT traversal tasks.

### Proof of Concept

```
1. Connect to victim node as peer A (attacker_peer_id).
2. Send ConnectionRequest{from=attacker_peer_id, to=victim_peer_id, listen_addrs=[<24 valid TCP addrs>]}.
   → victim inserts pending_delivered[attacker_peer_id] = ([addrs], now).
3. In a loop for i in 0..N:
     Send ConnectionSync{from=attacker_peer_id, to=victim_peer_id, route=[], item_id=i}.
     → passes rate_limiter (new bucket per item_id)
     → passes forward_rate_limiter (new bucket per item_id)
     → runtime::spawn fires, creating a task with 24 try_nat_traversal futures
4. After 30 seconds: N*24 sockets open, N async tasks running.
   Assert: open fd count > system ulimit → node crashes or becomes unresponsive.
```

### Citations

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

**File:** network/src/protocols/hole_punching/mod.rs (L28-28)
```rust
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L119-163)
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
                                    Status::ok()
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L65-68)
```rust
    let timeout_duration = Duration::from_secs(30);
    let start_time = Instant::now();
    let mut retry_count = 0u32;
    while start_time.elapsed() < timeout_duration {
```
