All code references check out. Let me verify the exact line numbers and constants before rendering the final output.

Audit Report

## Title
Unbounded `runtime::spawn` via Non-Consuming `.get()` on `pending_delivered` in `ConnectionSync` Handler — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

## Summary
When a victim CKB node is the `to` target of a `ConnectionSync` message, it looks up stored listen addresses via `.get()` on `pending_delivered` without ever removing the entry. Because the entry persists, an attacker who first floods the victim with `ConnectionRequest` messages using unique spoofed `from` peer IDs (populating `pending_delivered` without bound) can then repeatedly send matching `ConnectionSync` messages to trigger an uncapped number of `runtime::spawn` calls, each opening up to 24 concurrent `TcpSocket`s for 30 seconds, exhausting file descriptors and saturating the Tokio runtime.

## Finding Description

**Root cause — `.get()` instead of `.remove()` in `connection_sync.rs`:** [1](#0-0) 

The entry is read but never consumed. Every subsequent `ConnectionSync` with the same `from` peer ID finds the same entry and triggers another `runtime::spawn` at: [2](#0-1) 

There is no semaphore, counter, or cap on concurrent spawned tasks.

**Phase 1 — Populate `pending_delivered` without bound:**

`pending_delivered` is a plain unbounded `HashMap`: [3](#0-2) 

When the victim receives `ConnectionRequest(from=from_i, to=victim)`, `respond_delivered()` inserts into it: [4](#0-3) 

The deduplication guard at line 161–166 is keyed by `from_peer_id`; with unique `from_i` values it never fires: [5](#0-4) 

The `forward_rate_limiter` is keyed by `(from, to, item_id)` — also unique per `from_i`, so it never throttles: [6](#0-5) 

The per-session `rate_limiter` (30 req/s, keyed by `(session_id, item_id)`) is bypassable with multiple relay sessions: [7](#0-6) 

The `pending_delivered.retain(...)` cleanup runs only every `CHECK_INTERVAL = 5 minutes` with `TIMEOUT = 5 minutes`: [8](#0-7) 

**Phase 2 — Trigger unbounded spawns via `ConnectionSync`:**

The `forward_rate_limiter` check in `connection_sync.rs` is also keyed by `(from, to, item_id)` — unique per `from_i`, never fires: [9](#0-8) 

Each matching `ConnectionSync` reaches the uncapped `runtime::spawn`.

**Phase 3 — Each task exhausts resources:**

Each spawned task runs `select_ok` over up to `ADDRS_COUNT_LIMIT = 24` concurrent `try_nat_traversal` futures: [10](#0-9) 

Each `try_nat_traversal` future loops for up to 30 seconds, creating a new `TcpSocket` on every ~200 ms iteration: [11](#0-10) 

## Impact Explanation

**High — Vulnerabilities which could easily crash a CKB node.**

- **File descriptor exhaustion**: N concurrent tasks × 24 concurrent `try_nat_traversal` futures = N×24 open sockets at any moment. At N=500, ~12,000 concurrent sockets exceed the default Linux soft FD limit (1024). `TcpSocket::new_v4()`/`new_v6()` returns `EMFILE`/`ENFILE`, propagating errors into the p2p layer and crashing the node.
- **Async runtime saturation**: Each `runtime::spawn` occupies a Tokio task slot for up to 30 seconds. With hundreds of concurrent tasks, the runtime stalls, blocking all block/tx relay and peer management, causing the node to be dropped from the network.

## Likelihood Explanation

- Requires only standard P2P connections to the victim — no privileged access, no PoW, no keys.
- Unique `from_i` peer IDs are trivially generated (random 32-byte values).
- With 1 direct session: 30 `ConnectionRequest` entries/second → 500 entries in ~17 seconds. With K sessions: 30K entries/second.
- `pending_delivered` entries persist for 5 minutes, so Phase 1 runs once per 5-minute window. Phase 2 (`ConnectionSync` replay) can be repeated every 30 seconds to sustain the attack before tasks expire.
- The `forward_rate_limiter` (1/s per `(from, to, item_id)`) provides zero protection against unique `from_i` values.

## Recommendation

1. **Change `.get()` to `.remove()`** in `connection_sync.rs` line 114 so each `pending_delivered` entry triggers at most one spawn.
2. **Cap `pending_delivered` map size** — reject new `ConnectionRequest` entries once the map exceeds a threshold (e.g., 64 entries).
3. **Cap concurrent NAT traversal tasks** with a `tokio::sync::Semaphore` or bounded channel (e.g., max 8 concurrent tasks).
4. **Rate-limit `ConnectionSync` per `to` peer ID** on the victim, not just per `(from, to, item_id)`.

## Proof of Concept

```
Setup:
  - Attacker controls K P2P sessions to the victim (or relay nodes connected to it).
  - victim_peer_id is known (public).

Phase 1 (run once per 5 minutes):
  For i in 1..N:
    peer_id_i = random_bytes(32)
    Send ConnectionRequest(from=peer_id_i, to=victim, listen_addrs=[24 routable IPs])
    via session_j (round-robin across K sessions, 30 req/s each → 30K req/s total).
  → victim.pending_delivered now contains N entries (no size cap, no dedup for unique from_i).

Phase 2 (repeat every 30 seconds):
  For i in 1..N:
    Send ConnectionSync(from=peer_id_i, to=victim, route=[]) via any session.
  → victim calls runtime::spawn() N times (forward_rate_limiter bypassed by unique from_i).
  → Each task opens 24 concurrent TcpSockets, retrying every ~200ms for 30 seconds.

At N=500:
  - ~12,000 concurrent sockets → EMFILE on victim → node crash.
  - OR Tokio runtime saturated → node unresponsive → dropped from network.

Verification:
  - Monitor victim's open FD count: `ls /proc/<pid>/fd | wc -l`
  - Monitor Tokio task count via ckb_metrics or tokio-console.
  - Observe victim stops relaying blocks/transactions within seconds of Phase 2.
```

### Citations

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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-115)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L144-162)
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
```

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
