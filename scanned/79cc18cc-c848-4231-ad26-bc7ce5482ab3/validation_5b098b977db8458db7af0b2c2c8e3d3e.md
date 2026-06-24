Audit Report

## Title
Unbounded `runtime::spawn` via Non-Consuming `pending_delivered` Lookup in `ConnectionSync` Handler — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

## Summary
An unprivileged remote attacker can cause a victim CKB node to spawn an unbounded number of long-running async tasks by: (1) flooding the victim with `ConnectionRequest` messages using unique spoofed `from` peer IDs to populate `pending_delivered`, then (2) repeatedly sending matching `ConnectionSync` messages that each trigger an uncapped `runtime::spawn`. Because `connection_sync.rs` uses `.get()` instead of `.remove()` on `pending_delivered`, each entry is never consumed and can trigger arbitrarily many spawns, exhausting file descriptors and the Tokio async runtime.

## Finding Description

**Root cause — `.get()` instead of `.remove()` in `connection_sync.rs`:**

When the victim is the `to` target of a `ConnectionSync` message, it looks up the stored listen addresses:

```rust
// connection_sync.rs L111-115
let listens_info = self
    .protocol
    .pending_delivered
    .get(&content.from)
    .map(|info| info.0.clone());
```

The entry is read but **never removed**. Every subsequent `ConnectionSync` with the same `from` peer ID will find the same entry and trigger another `runtime::spawn`.

**Phase 1 — Populate `pending_delivered` without bound:**

When the victim receives `ConnectionRequest(from=from_i, to=victim)`, `respond_delivered()` inserts into `pending_delivered`:

```rust
// connection_request.rs L161-167
if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
    let now = unix_time_as_millis();
    if now - t < HOLE_PUNCHING_INTERVAL {
        return StatusCode::Ignore ...
    }
}
// ...
// connection_request.rs L234-237
self.protocol
    .pending_delivered
    .insert(from_peer_id, (remote_listens, now));
```

The deduplication guard is keyed by `from_peer_id`. With unique `from_i` values (random bytes), it never fires. The `forward_rate_limiter` is keyed by `(from, to, item_id)` — also unique per `from_i`. The per-session `rate_limiter` (30 req/s) is bypassable by using multiple relay sessions. The `pending_delivered` map has **no size cap**.

**Phase 2 — Trigger unbounded spawns via `ConnectionSync`:**

For each `from_i` in `pending_delivered`, sending `ConnectionSync(from=from_i, to=victim)` causes:

```rust
// connection_sync.rs L144-163
runtime::spawn(async move {
    if let Ok(((stream, addr), _)) = select_ok(tasks).await {
        ...
    }
});
```

There is no semaphore, counter, or cap on concurrent spawned tasks.

**Phase 3 — Each task exhausts resources:**

Each spawned task runs `select_ok` over up to `ADDRS_COUNT_LIMIT=24` concurrent `try_nat_traversal` futures. Each future loops for up to 30 seconds, creating a new `TcpSocket` on every ~200 ms iteration:

```rust
// component/mod.rs L65-80
let timeout_duration = Duration::from_secs(30);
let start_time = Instant::now();
while start_time.elapsed() < timeout_duration {
    let socket = create_socket(bind_addr, net_addr)?;
    match runtime::timeout(Duration::from_millis(200), socket.connect(net_addr)).await { ... }
    runtime::delay_for(actual_interval).await;
}
```

**Why existing guards fail:**

- `forward_rate_limiter` (1/s per `(from, to, item_id)`): unique `from_i` values make every key distinct — no throttling.
- Per-session `rate_limiter` (30/s per `(session_id, item_id)`): bypassable with multiple relay sessions.
- `pending_delivered.retain(...)` cleanup: runs only every `CHECK_INTERVAL = 5 minutes` with `TIMEOUT = 5 minutes`, giving the attacker a 5-minute window to accumulate entries and replay `ConnectionSync` indefinitely.

## Impact Explanation

**High — Vulnerabilities which could easily crash a CKB node.**

- **File descriptor exhaustion**: N concurrent tasks × 24 concurrent `try_nat_traversal` futures = N×24 open sockets at any moment. At N=500, ~12,000 concurrent sockets exceed typical OS FD limits (default 1024, max ~65535 on Linux). `TcpSocket::new_*` returns `EMFILE`/`ENFILE`, propagating errors into the p2p layer.
- **Async runtime saturation**: Each `runtime::spawn` occupies a Tokio task slot for up to 30 seconds. With hundreds of concurrent tasks, the runtime stalls, blocking all block/tx relay and peer management, causing the node to be dropped from the network.
- Either path results in a node crash or complete unresponsiveness.

## Likelihood Explanation

- Requires only standard P2P connections to relay nodes — no privileged access, no PoW, no keys.
- Unique `from_i` peer IDs are trivially generated (random 32-byte values).
- The attack is repeatable: `pending_delivered` entries persist for 5 minutes, so Phase 1 only needs to run once per 5-minute window. Phase 2 (sending `ConnectionSync`) can be repeated every 30 seconds to sustain the attack before tasks expire.
- The attacker needs K relay sessions to overcome the 30 req/s per-session cap; K=2 already doubles throughput.

## Recommendation

1. **Change `.get()` to `.remove()`** in `connection_sync.rs` L114 so each `pending_delivered` entry triggers at most one spawn.
2. **Cap `pending_delivered` map size** — reject new `ConnectionRequest` entries once the map exceeds a threshold (e.g., 64 entries).
3. **Cap concurrent NAT traversal tasks** with a `tokio::sync::Semaphore` or bounded channel (e.g., max 8 concurrent tasks).
4. **Rate-limit `ConnectionSync` per `to` peer ID** on the victim, not just per `(from, to, item_id)`.

## Proof of Concept

```
Setup:
  - Attacker controls K P2P sessions to relay nodes connected to the victim.
  - victim_peer_id is known (public).

Phase 1 (run once per 5 minutes):
  For i in 1..N:
    peer_id_i = random_bytes(32)
    Send ConnectionRequest(from=peer_id_i, to=victim, listen_addrs=[24 routable IPs])
    via relay_j (round-robin across K sessions, 30 req/s each → 30K req/s total).
  → victim.pending_delivered now contains N entries.

Phase 2 (repeat every 30 seconds):
  For i in 1..N:
    Send ConnectionSync(from=peer_id_i, to=victim, route=[]) via any relay.
  → victim calls runtime::spawn() N times.
  → Each task opens 24 concurrent TcpSockets, retrying every ~200ms for 30 seconds.

At N=500:
  - ~12,000 concurrent sockets → EMFILE on victim → node crash.
  - OR Tokio runtime saturated → node unresponsive → dropped from network.

Verification:
  - Monitor victim's open FD count: `ls /proc/<pid>/fd | wc -l`
  - Monitor Tokio task count via ckb_metrics or tokio-console.
  - Observe victim stops relaying blocks/transactions within seconds of Phase 2.
```