Based on my full read of all four relevant files, here is my analysis:

---

### Title
Unbounded Async Task Spawn via Unauthenticated `ConnectionSync` + Attacker-Controlled `pending_delivered` — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

### Summary

An unprivileged peer connected to a victim CKB node can:
1. Pre-populate `pending_delivered` with M unique `from_peer_id` entries (each holding up to 24 TCP addresses) by sending M crafted `ConnectionRequest` messages.
2. Send M `ConnectionSync` messages (one per unique `from_peer_id`), each causing the victim to call `runtime::spawn(select_ok(24 try_nat_traversal tasks))`.

There is no guard preventing this because `ConnectionSync` never verifies that the sender is the actual `content.from` peer, and there is no cap on concurrent NAT traversal tasks.

---

### Finding Description

**Phase 1 — Populate `pending_delivered`**

In `connection_request.rs`, `respond_delivered()` is called when `self_peer_id == &content.to`. It stores attacker-supplied addresses into `pending_delivered`: [1](#0-0) 

The only guard against re-use of the same `from_peer_id` is a 2-minute cooldown: [2](#0-1) 

With M **distinct** fake `from_peer_id` values, the attacker bypasses this entirely. The general rate limiter allows 30 messages/sec per session: [3](#0-2) 

So from a single session the attacker can insert 30 entries/sec into `pending_delivered`. Entries persist for 5 minutes (`TIMEOUT`): [4](#0-3) 

This allows accumulation of up to ~9,000 entries before the first ones expire.

**Phase 2 — Trigger unbounded task spawns via `ConnectionSync`**

`ConnectionSyncProcess::execute()` checks whether the victim is the `to` target, then looks up `pending_delivered[content.from]` and unconditionally spawns a task: [5](#0-4) 

There is **no check** that the sender of the `ConnectionSync` is the actual `content.from` peer. Any connected peer can set `content.from` to any value present in `pending_delivered`. The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)`: [6](#0-5) 

With M unique `from_peer_ids`, M distinct rate-limit buckets are created, so M `ConnectionSync` messages per second pass through (bounded only by the 30/sec per-session cap).

**Phase 3 — Each spawned task is expensive**

Each `try_nat_traversal` task runs for up to 30 seconds, creating a new TCP socket and attempting a connection every ~200ms (~150 retries): [7](#0-6) 

With 30 `ConnectionSync` messages/sec × 24 tasks each = **720 new async tasks/sec**, each living 30 seconds → **~21,600 concurrent tasks at steady state** from a single attacker session.

**Contrast with `ConnectionRequestDelivered`**: that handler requires `inflight_requests.remove(&content.to)` to succeed (victim must have previously initiated a request), providing a meaningful guard: [8](#0-7) 

`ConnectionSync` has no equivalent guard.

---

### Impact Explanation

Each spawned task holds an async runtime slot for 30 seconds and makes repeated TCP `connect()` syscalls. At 720 tasks/sec from one attacker session, the Tokio runtime's thread pool and OS socket/fd limits are exhausted, causing the victim node to stop processing all other P2P messages (sync, block relay, transaction relay), effectively taking it offline. Multiple attacker sessions scale this linearly.

---

### Likelihood Explanation

The attacker needs only a single standard P2P connection to the victim. No PoW, no privileged role, no key material. The two-phase attack (populate then trigger) is straightforward to implement. The `ADDRS_COUNT_LIMIT = 24` constant maximizes the per-message task count. [9](#0-8) 

---

### Recommendation

1. **Verify sender identity in `ConnectionSync`**: confirm the session sending the `ConnectionSync` corresponds to `content.from` (i.e., `context.session` peer ID equals `content.from`).
2. **Cap concurrent NAT traversal tasks**: maintain a global counter; reject spawns beyond a small limit (e.g., 4–8 concurrent tasks).
3. **Bound `pending_delivered` size**: enforce a maximum map size (e.g., 64 entries) and evict oldest on overflow.
4. **Require `inflight_requests` for `ConnectionSync`**: mirror the guard used in `ConnectionRequestDelivered` — only process `ConnectionSync` if the victim previously initiated a request for `content.from`.

---

### Proof of Concept

```
1. Attacker connects to victim V (peer ID = V_id).

2. For i in 1..M:
   - Generate fake_id_i = random PeerId
   - Send ConnectionRequest { from=fake_id_i, to=V_id,
       listen_addrs=[24 valid TCP IP:port], route=[], max_hops=6 }
   - V stores pending_delivered[fake_id_i] = ([24 addrs], now)
   (rate: 30/sec per session → M entries in M/30 seconds)

3. For i in 1..M:
   - Send ConnectionSync { from=fake_id_i, to=V_id, route=[] }
   - V: self_peer_id == content.to ✓
   - V: pending_delivered.get(fake_id_i) → Some([24 addrs]) ✓
   - V: runtime::spawn(select_ok(24 try_nat_traversal tasks))
   (rate: 30/sec → 720 tasks/sec, 21,600 concurrent at steady state)

4. Assert: V's async runtime is saturated; P2P message processing stalls.
```

### Citations

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

**File:** network/src/protocols/hole_punching/mod.rs (L27-27)
```rust
const ADDRS_COUNT_LIMIT: usize = 24;
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

**File:** network/src/protocols/hole_punching/mod.rs (L173-175)
```rust
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-163)
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
                                    Status::ok()
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L65-115)
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
}
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
