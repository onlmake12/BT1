## Full Code Trace

**`pending_delivered` population** — `connection_request.rs` `respond_delivered()`: [1](#0-0) 

`from_peer_id` is taken directly from the message field with **no validation that it matches the sender's authenticated session PeerId**. An attacker can set `from` to any arbitrary PeerId.

**`ConnectionSync` spawn trigger** — `connection_sync.rs` `execute()`: [2](#0-1) 

**`try_nat_traversal` timeout** — `component/mod.rs`: [3](#0-2) 

Each task loops for up to 30 seconds, creating a new `TcpSocket` on every ~200ms iteration.

**Rate limiters**: [4](#0-3) [5](#0-4) 

The outer limiter allows 30 `ConnectionSync` messages/sec per session. The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)` — 1/sec per unique `(from, to)` pair. With N different `from` PeerIds in `pending_delivered`, N spawns/sec are possible (up to 30/sec from one session).

**No cap on spawned tasks or `pending_delivered` size**: [6](#0-5) [7](#0-6) 

`pending_delivered` is an unbounded `HashMap`. `runtime::spawn` is called with no semaphore or task count guard.

**`pending_delivered` cleanup** runs only every 5 minutes: [8](#0-7) [9](#0-8) 

---

### Title
Unbounded `runtime::spawn` via spoofed `ConnectionSync` from PeerId — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

### Summary
An unprivileged remote peer can exhaust the async runtime and file descriptors by first populating the victim's `pending_delivered` map with spoofed `from` PeerIds via `ConnectionRequest` messages, then sending `ConnectionSync` messages with those PeerIds to trigger an unbounded number of `runtime::spawn(select_ok(24 TCP tasks))` calls, each lasting up to 30 seconds.

### Finding Description

**Phase 1 — Populate `pending_delivered` with spoofed entries**

The attacker sends `ConnectionRequest` messages with `to=victim_peer_id`, `route=[]`, and `from=<arbitrary_spoofed_peer_id>` plus up to 24 attacker-controlled TCP listen addresses. In `connection_request.rs` `respond_delivered()`, the `from` field is accepted verbatim from the message with no check that it matches the sender's authenticated session PeerId: [10](#0-9) 

The only guard is a 2-minute cooldown per `from` PeerId. With different spoofed `from` PeerIds each time, the attacker bypasses this and populates `pending_delivered` with up to 30 new entries per second (bounded only by the outer 30/sec per-session rate limiter). The map has no size cap.

**Phase 2 — Trigger unbounded spawns via `ConnectionSync`**

For each `from` PeerId in `pending_delivered`, the attacker sends `ConnectionSync` with `route=[]`, `to=victim_peer_id`, `from=<that_peer_id>`. The `forward_rate_limiter` key is `(from, to, msg_item_id)` — 1/sec per pair. With N different `from` PeerIds, N spawns/sec are possible: [11](#0-10) 

Each `runtime::spawn` runs `select_ok(tasks)` where `tasks` contains up to `ADDRS_COUNT_LIMIT=24` `try_nat_traversal` futures: [12](#0-11) 

Each `try_nat_traversal` future loops for up to 30 seconds, creating a new `TcpSocket` on every ~200ms iteration (~150 sockets per task over its lifetime): [13](#0-12) 

There is no semaphore, no task count limit, and no cancellation of previously spawned tasks.

### Impact Explanation

**Steady-state resource consumption** (single attacker session, 30 spawns/sec, 30-second task lifetime):
- Concurrent spawned tasks: 30 × 30 = **900 tasks**
- Concurrent `try_nat_traversal` futures: 900 × 24 = **21,600**
- Each future creates a new `TcpSocket` every ~200ms → **~108,000 socket operations/sec**
- Open file descriptors: easily exceeds typical OS limits (1024–65536), causing `EMFILE`/`ENFILE` errors that crash the node's ability to accept any new connections

This constitutes async runtime task exhaustion and file descriptor exhaustion, crashing the victim node's P2P networking entirely.

### Likelihood Explanation

- Attacker only needs a single P2P connection to the victim (unprivileged, no PoW, no keys)
- `from` PeerId spoofing requires no special knowledge — any valid-length byte sequence works
- The attack is repeatable and self-sustaining within the 5-minute `pending_delivered` TTL window
- The `forward_rate_limiter` is the only throttle, and it is fully bypassed by using distinct `from` PeerIds

### Recommendation

1. **Validate `from` against sender's session PeerId** in `ConnectionRequest` handling: reject any message where `content.from` does not match the authenticated PeerId of the sending session.
2. **Cap `pending_delivered` size** (e.g., max 64 entries, evicting oldest on overflow).
3. **Bound concurrent NAT traversal tasks** with a global semaphore (e.g., `tokio::sync::Semaphore` with a limit of 8–16 concurrent tasks).
4. **Cancel the previous spawned task** for a `(from, to)` pair before spawning a new one.

### Proof of Concept

```
1. Attacker connects to victim via P2P (one session).
2. For i in 0..N:
     Send ConnectionRequest {
       from: random_peer_id_i,   // spoofed, not attacker's real PeerId
       to: victim_peer_id,
       route: [],
       listen_addrs: [attacker_ip:port_1, ..., attacker_ip:port_24],  // 24 addrs
       max_hops: 6
     }
   → victim inserts random_peer_id_i into pending_delivered
   (rate: up to 30/sec via outer rate_limiter)

3. For i in 0..N:
     Send ConnectionSync {
       from: random_peer_id_i,   // matches pending_delivered entry
       to: victim_peer_id,
       route: []
     }
   → victim calls runtime::spawn(select_ok(24 try_nat_traversal tasks))
   (rate: 1/sec per (from,to) pair; N pairs → N spawns/sec)

4. After 30 seconds: 30*N spawned tasks alive, each holding up to 24 TCP sockets.
   Assert: open FD count on victim exceeds OS limit → node crashes.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L155-167)
```rust
    async fn respond_delivered(
        &mut self,
        from_peer_id: PeerId,
        to_peer_id: &PeerId,
        remote_listens: Vec<Multiaddr>,
    ) -> Status {
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

**File:** network/src/protocols/hole_punching/mod.rs (L25-25)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
```

**File:** network/src/protocols/hole_punching/mod.rs (L27-27)
```rust
const ADDRS_COUNT_LIMIT: usize = 24;
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
