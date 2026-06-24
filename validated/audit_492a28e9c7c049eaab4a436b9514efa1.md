Audit Report

## Title
Unbounded NAT Traversal Task Spawn via Unauthenticated `ConnectionSync` and Attacker-Populated `pending_delivered` — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

## Summary

An unprivileged remote peer can pre-populate the victim's `pending_delivered` map with attacker-controlled TCP addresses by sending `ConnectionRequest` messages with unique `from_peer_id` values, then trigger unbounded `runtime::spawn` calls by sending `ConnectionSync` messages whose `content.from` matches those entries. Unlike `ConnectionRequestDeliveredProcess`, `ConnectionSyncProcess::execute()` contains no `inflight_requests` guard, so any `ConnectionSync` whose `content.from` is present in `pending_delivered` unconditionally spawns a long-lived NAT traversal task. At the outer rate limit of 30/sec, a single attacker session sustains 900 concurrent spawned tasks over 30 seconds, each holding up to 24 concurrent async futures making repeated TCP socket operations, leading to file descriptor exhaustion and async runtime saturation.

## Finding Description

**Phase 1 — Populating `pending_delivered`**

When the victim is the `to` target of a `ConnectionRequest`, `respond_delivered()` stores attacker-supplied `listen_addrs` into `pending_delivered` keyed by `from_peer_id`: [1](#0-0) 

The only re-insertion guard is a per-`from_peer_id` cooldown of `HOLE_PUNCHING_INTERVAL = 2 minutes`: [2](#0-1) 

The `from_peer_id` field is fully attacker-controlled — it is arbitrary bytes parsed from the message with no verification that the peer is actually connected. With M unique `from_peer_ids`, M independent entries are inserted. The `forward_rate_limiter` is keyed on `(from, to, msg_item_id)`: [3](#0-2) 

With unique `from` values, each gets its own rate-limit bucket (1/sec each). The binding constraint is the outer `rate_limiter` at 30/sec per `(session_id, msg_item_id)`: [4](#0-3) 

Over the 5-minute `TIMEOUT` window, a single attacker session can accumulate up to `30 × 300 = 9,000` entries in `pending_delivered`, each holding up to `ADDRS_COUNT_LIMIT = 24` TCP addresses: [5](#0-4) 

**Phase 2 — Triggering unbounded spawns via `ConnectionSync`**

When a `ConnectionSync` arrives with `content.to == self_peer_id` and `content.from` present in `pending_delivered`, the code unconditionally spawns a task: [6](#0-5) 

Critically, `ConnectionRequestDeliveredProcess::execute()` gates NAT traversal on `inflight_requests.remove(&content.to)`: [7](#0-6) 

`ConnectionSyncProcess::execute()` has **no such guard**. It does not verify that the victim ever initiated a hole-punching session for this `from_peer_id`. Any `ConnectionSync` whose `content.from` matches a `pending_delivered` entry unconditionally calls `runtime::spawn`.

The `forward_rate_limiter` in `ConnectionSyncProcess` is keyed on `(content.from, content.to, msg_item_id)`: [8](#0-7) 

With M unique `from_peer_ids` pre-populated in `pending_delivered`, M independent rate-limit buckets exist (1/sec each), so the effective cap is the outer `rate_limiter` at 30/sec per session.

**Phase 3 — Resource cost per spawned task**

Each `try_nat_traversal` future runs for up to 30 seconds, creating a new `TcpSocket` per retry at ~200ms intervals (~150 retries): [9](#0-8) 

With 24 addresses per spawn, each spawned task generates ~3,600 socket operations over its lifetime.

## Impact Explanation

From a single attacker session at 30 `ConnectionSync` messages/sec, over the 30-second task lifetime: 30 × 30 = 900 concurrent spawned tasks, each with 24 concurrent async futures = **21,600 concurrent async futures**. Each future makes ~150 TCP socket operations → ~3.24 million socket operations over 30 seconds. This causes:

1. **File descriptor exhaustion** — each TCP connection attempt opens a socket
2. **Async runtime saturation** — 21,600 concurrent futures polling on a shared executor
3. **Network congestion** — outbound TCP SYN floods to attacker-controlled addresses

This matches the **High (10001–15000 points)** impact class: *Vulnerabilities which could easily crash a CKB node*.

## Likelihood Explanation

The attack requires only a standard P2P connection — no special privileges, no PoW, no key material. The `from_peer_id` and `listen_addrs` fields in `ConnectionRequest` are fully attacker-controlled. The two-phase setup is straightforward: populate `pending_delivered` over ~300 seconds, then sustain `ConnectionSync` spam indefinitely. The rate limits slow but do not prevent the attack. Multiple attacker sessions scale the impact linearly.

## Recommendation

1. **Add an `inflight_requests` guard in `ConnectionSyncProcess::execute()`** — mirror the check in `ConnectionRequestDeliveredProcess`: only proceed if `inflight_requests` contains `content.from` (the peer the victim initiated hole-punching toward), and remove the entry on use.

2. **Bound `pending_delivered` map size** — cap the total number of entries (e.g., 256) to prevent unbounded memory growth regardless of rate limiting.

3. **Add a global cap on concurrent NAT traversal tasks** — use a semaphore or atomic counter to limit total in-flight `runtime::spawn` NAT traversal tasks across both `ConnectionSync` and `ConnectionRequestDelivered` paths.

## Proof of Concept

```
1. Attacker connects to victim as a normal P2P peer.

2. Attacker sends 30 ConnectionRequest messages/sec for 300 seconds, each with:
   - content.to = victim_peer_id
   - content.from = unique_peer_id_N  (N = 1..9000, arbitrary bytes)
   - content.listen_addrs = [24 valid TCP/IPv4 addresses under attacker control]
   → victim stores 9,000 entries in pending_delivered[peer_id_N] = ([24 addrs], now)

3. Attacker sends 30 ConnectionSync messages/sec indefinitely, each with:
   - content.to = victim_peer_id
   - content.from = peer_id_N  (cycling through pending_delivered entries)
   - content.route = []
   → victim calls runtime::spawn(select_ok(24 try_nat_traversal tasks)) for each

4. After 30 seconds: 900 spawned tasks × 24 futures = 21,600 concurrent async futures
   making TCP connection attempts to attacker-controlled addresses.

5. Observable result: victim node's file descriptor table exhausts (ulimit -n typically
   1024–65536), async runtime stalls, node becomes unresponsive to legitimate peers.
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
