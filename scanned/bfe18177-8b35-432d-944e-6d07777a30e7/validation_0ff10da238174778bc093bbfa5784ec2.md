Audit Report

## Title
Unbounded `runtime::spawn` via Non-Consuming `pending_delivered` Lookup in `ConnectionSync` — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

## Summary
An unprivileged remote attacker can cause a victim CKB node to spawn an unbounded number of long-running async tasks by: (1) flooding the victim with `ConnectionRequest` messages using unique spoofed `from` peer IDs to populate the unbounded `pending_delivered` map, then (2) repeatedly sending matching `ConnectionSync` messages that trigger `runtime::spawn` on the victim for each entry. Each spawned task runs for up to 30 seconds and opens up to 24 TCP sockets, leading to file descriptor exhaustion and async runtime saturation, crashing the node.

## Finding Description

**Root cause — `.get()` instead of `.remove()` in `connection_sync.rs`:**

When the victim receives a `ConnectionSync` targeting itself, it looks up `pending_delivered` using `.get()`: [1](#0-0) 

Because `.get()` does not consume the entry, the same `from` peer ID can trigger `runtime::spawn` an unlimited number of times. Each call spawns a new async task: [2](#0-1) 

**Phase 1 — Populate `pending_delivered` without bound:**

The victim inserts into `pending_delivered` for every unique `from_peer_id` it receives in a `ConnectionRequest`: [3](#0-2) 

The only guard is a per-`from_peer_id` interval check: [4](#0-3) 

With unique `from_i` peer IDs, this check never fires. The `pending_delivered` map is a plain `HashMap` with no size cap: [5](#0-4) 

**Phase 2 — Rate limiters are ineffective:**

The `forward_rate_limiter` is keyed by `(from, to, item_id)` at 1 req/s: [6](#0-5) 

With unique `from_i`, every key is fresh and the limiter never triggers. The per-session `rate_limiter` (30 req/s) can be multiplied across K relay sessions.

**Phase 3 — Each spawned task exhausts resources:**

Each `runtime::spawn` runs `select_ok` over up to `ADDRS_COUNT_LIMIT=24` futures, each calling `try_nat_traversal`. That function loops for up to 30 seconds, creating a new `TcpSocket` on every ~200 ms iteration: [7](#0-6) 

**Phase 4 — Cleanup window is 5 minutes:**

`pending_delivered` entries are only evicted every 5 minutes: [8](#0-7) 

During this window, the attacker can accumulate thousands of entries and replay `ConnectionSync` for each, sustaining the attack indefinitely.

## Impact Explanation

**High — Vulnerabilities which could easily crash a CKB node.**

- **File descriptor exhaustion**: N concurrent tasks × 24 addresses × ~150 socket attempts per task lifetime = O(N×3600) open FDs. At N=500 this exceeds typical OS limits (1024–65535), causing `EMFILE`/`ENFILE` errors that propagate into the p2p layer.
- **Async runtime saturation**: Each `runtime::spawn` occupies a Tokio thread pool slot for up to 30 seconds. With sufficient concurrent tasks the runtime stalls, blocking all block/tx relay and causing the node to be dropped from the network.
- Either path results in a node crash or complete unresponsiveness.

## Likelihood Explanation

- Attacker requires only standard P2P connections to relay nodes — no privileged access, no PoW, no keys.
- Unique `from_i` peer IDs are trivially generated (random bytes).
- The `pending_delivered` map persists entries for 5 minutes, so Phase 1 only needs to run once per 5-minute window.
- Phase 2 (replaying `ConnectionSync`) can be repeated every 30 seconds to sustain the attack before tasks expire.
- The `forward_rate_limiter` provides zero protection against unique-`from` flooding.

## Recommendation

1. **Change `.get()` to `.remove()`** in `connection_sync.rs` L114 so each `pending_delivered` entry can only trigger one `runtime::spawn`.
2. **Cap `pending_delivered` map size** — reject new `ConnectionRequest` entries once the map exceeds a threshold (e.g., 64 entries).
3. **Cap concurrent NAT traversal tasks** with a semaphore or bounded channel (e.g., max 4–8 concurrent tasks globally).
4. **Rate-limit `ConnectionSync` per `to` peer ID** (the victim), not just per `(from, to, item_id)` triple.

## Proof of Concept

```
1. Attacker connects to relay_1, ..., relay_K (K sessions, each allows 30 req/s).
2. For i in 1..N:
     Send ConnectionRequest(from=random_peer_id_i, to=victim, listen_addrs=[24 routable IPs])
     via relay_j (round-robin).
   → victim.pending_delivered now has N entries (no size limit, unique keys bypass rate limiter).
3. For i in 1..N:
     Send ConnectionSync(from=random_peer_id_i, to=victim, route=[]) via any relay.
   → victim calls runtime::spawn() N times (entry not consumed by .get()).
   → Each task opens up to 24 TcpSockets and loops for 30 seconds.
4. At N=500: ~12,000 concurrent sockets → EMFILE or runtime stall → node crash.
5. Repeat step 3 every 30 seconds to sustain the attack.
   pending_delivered entries persist for 5 minutes, so step 2 only needs to run once per 5 min.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-115)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L144-163)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L44-44)
```rust
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
```

**File:** network/src/protocols/hole_punching/mod.rs (L173-175)
```rust
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/mod.rs (L255-257)
```rust
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
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
