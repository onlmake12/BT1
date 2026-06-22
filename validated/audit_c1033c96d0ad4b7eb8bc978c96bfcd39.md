I have a complete picture of the code. Let me analyze the full attack path carefully.

### Title
Unbounded NAT Traversal Task Spawn via Attacker-Controlled `pending_delivered` and Unauthenticated `ConnectionSync` — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

---

### Summary

An unprivileged remote peer can pre-populate the victim's `pending_delivered` map with up to 24 attacker-controlled TCP addresses per unique `from_peer_id`, then send `ConnectionSync` messages to trigger `runtime::spawn(select_ok(N tasks))` for each entry. Because `ConnectionSync` processing has no guard against `inflight_requests` (unlike `ConnectionRequestDelivered`), and because the `forward_rate_limiter` is keyed per `(from, to)` pair allowing unbounded unique pairs, an attacker can sustain 720 concurrent long-lived NAT traversal futures per second from a single session, exhausting async runtime resources and file descriptors.

---

### Finding Description

**Phase 1 — Populating `pending_delivered`**

When the victim node is the `to` target of a `ConnectionRequest`, `respond_delivered()` stores the attacker-supplied `listen_addrs` into `pending_delivered`: [1](#0-0) 

The only re-insertion guard is per-`from_peer_id` with a 2-minute cooldown: [2](#0-1) 

The `from_peer_id` field is **fully attacker-controlled** — it is arbitrary bytes parsed from the message with no verification that the peer actually exists or is connected. With M unique `from_peer_ids`, M independent entries are inserted. The outer `rate_limiter` caps at 30 `ConnectionRequest` messages per second per session: [3](#0-2) 

Over the 5-minute `TIMEOUT` window, a single attacker session can accumulate up to `30 × 300 = 9,000` entries in `pending_delivered`, each holding up to 24 TCP addresses (`ADDRS_COUNT_LIMIT = 24`): [4](#0-3) 

**Phase 2 — Triggering NAT traversal spawns via `ConnectionSync`**

When a `ConnectionSync` arrives with `content.to == self_peer_id` and `content.from` present in `pending_delivered`, the code unconditionally spawns a task: [5](#0-4) 

Critically, unlike `ConnectionRequestDeliveredProcess::execute()` which gates on `inflight_requests.remove(&content.to)`: [6](#0-5) 

`ConnectionSyncProcess::execute()` has **no such guard**. It does not verify that the victim ever initiated a hole-punching session for this `from_peer_id`. Any `ConnectionSync` message whose `content.from` matches a `pending_delivered` entry unconditionally spawns tasks.

The `forward_rate_limiter` is keyed on `(from, to, msg_item_id)`: [7](#0-6) 

With M unique `from_peer_ids`, M independent rate-limit buckets exist, so the effective cap is the outer `rate_limiter` at 30/sec per session.

**Phase 3 — Resource cost of each spawned task**

Each `try_nat_traversal` future runs for up to 30 seconds, retrying TCP connections every ~200ms: [8](#0-7) 

Each retry creates a new `TcpSocket` (file descriptor). With 24 addresses per spawn and a 30-second lifetime, a single spawned task generates ~75 socket operations per address = 1,800 socket operations total.

---

### Impact Explanation

From a single attacker session:
- **30 `ConnectionSync` messages/sec** → 30 `runtime::spawn` calls/sec
- Each spawn contains **24 concurrent `try_nat_traversal` futures**
- Over the 30-second task lifetime: **30 × 30 = 900 concurrent spawned tasks**, each with 24 futures = **21,600 concurrent async futures**
- Each future makes ~75 TCP socket operations → **~1.6 million socket operations over 30 seconds**

This causes:
1. **File descriptor exhaustion** — each TCP connection attempt opens a socket
2. **Async runtime saturation** — 21,600 concurrent futures polling on a shared executor
3. **Network congestion** — outbound TCP SYN floods to attacker-controlled addresses

With multiple attacker sessions (the victim accepts multiple inbound connections), the impact scales linearly.

---

### Likelihood Explanation

The attack requires only a standard P2P connection to the victim — no special privileges, no PoW, no key material. The `from_peer_id` and `listen_addrs` fields in `ConnectionRequest` are fully attacker-controlled. The two-phase setup (populate then trigger) is straightforward and locally testable. The rate limits slow but do not prevent the attack.

---

### Recommendation

1. **Add an `inflight_requests` guard in `ConnectionSyncProcess::execute()`** — mirror the check in `ConnectionRequestDeliveredProcess`: only proceed if `inflight_requests` contains `content.to`, and remove the entry on use. This ensures NAT traversal is only triggered for sessions the victim itself initiated.

2. **Bound `pending_delivered` map size** — cap the total number of entries (e.g., 256) to prevent unbounded memory and task growth regardless of rate limiting.

3. **Add a global cap on concurrent NAT traversal tasks** — use a semaphore or task counter to limit total in-flight `runtime::spawn` NAT traversal tasks.

---

### Proof of Concept

```
1. Attacker connects to victim as a normal P2P peer.

2. Attacker sends 30 ConnectionRequest messages per second, each with:
   - content.to = victim_peer_id
   - content.from = unique_peer_id_N  (N = 1..M, arbitrary bytes)
   - content.listen_addrs = [24 valid TCP/IPv4 addresses]
   → victim stores M entries in pending_delivered[peer_id_N] = ([24 addrs], now)

3. After accumulating M entries (takes M/30 seconds), attacker sends M ConnectionSync
   messages per second, each with:
   - content.to = victim_peer_id
   - content.from = peer_id_N  (matching a pending_delivered entry)
   - content.route = []
   → victim calls runtime::spawn(select_ok(24 try_nat_traversal tasks)) for each

4. Assert: after 30 seconds, victim has 900 spawned tasks × 24 futures = 21,600
   concurrent async futures making TCP connection attempts, exhausting fd table
   and async runtime capacity.
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

**File:** network/src/protocols/hole_punching/mod.rs (L27-28)
```rust
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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L160-176)
```rust
                    let request_start = self.protocol.inflight_requests.remove(&content.to);

                    match request_start {
                        Some(start) => {
                            let res = self.respond_sync(content.from).await;
                            if !res.is_ok() {
                                return res;
                            }
                            let now = unix_time_as_millis();
                            let ttl = now - start;

                            self.try_nat_traversal(ttl, content.listen_addrs);

                            Status::ok()
                        }
                        None => StatusCode::Ignore.with_context("the request is not in flight"),
                    }
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
