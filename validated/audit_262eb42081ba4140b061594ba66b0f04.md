Audit Report

## Title
Unauthenticated `from` Field Enables Unbounded `runtime::spawn` NAT Traversal Tasks via Spoofed `ConnectionRequest`/`ConnectionSync` — (`network/src/protocols/hole_punching/component/connection_request.rs`, `connection_sync.rs`)

## Summary
`ConnectionRequestProcess::execute` stores attacker-controlled addresses into `pending_delivered` keyed by the message's `from` field without verifying that `from` matches the actual session's peer ID. A single connected peer can flood `pending_delivered` with up to 30 distinct entries per second (each holding up to 24 TCP addresses), then trigger an unbounded number of `runtime::spawn` calls via `ConnectionSync` messages. Each spawned task runs 24 concurrent `try_nat_traversal` futures that each retry TCP connections for 30 seconds with no global concurrency cap, exhausting file descriptors and crashing the node.

## Finding Description

**Root cause — unauthenticated `from` in `ConnectionRequestProcess::execute`:**

`ConnectionRequestProcess` holds `self.peer: PeerIndex` (the actual session), but `execute()` never checks that `content.from` equals the peer ID associated with `self.peer`. [1](#0-0) 

`respond_delivered` inserts `from_peer_id` (fully attacker-controlled) as the key into `pending_delivered`: [2](#0-1) 

The only per-`from` guard is `HOLE_PUNCHING_INTERVAL` (2 minutes), trivially bypassed by using a fresh `from` value per message: [3](#0-2) 

The `forward_rate_limiter` is keyed by `(content.from, content.to, item_id)`, so each distinct spoofed `from` value gets its own independent 1/sec bucket: [4](#0-3) 

The top-level `rate_limiter` is keyed by `(session_id, msg.item_id())` and allows 30/sec — so 30 distinct `pending_delivered` entries can be inserted per second from a single session: [5](#0-4) 

`ADDRS_COUNT_LIMIT` caps each entry at 24 addresses: [6](#0-5) 

**Unbounded `runtime::spawn` in `ConnectionSyncProcess::execute`:**

When `content.to == local_peer_id`, the code looks up `pending_delivered[content.from]` and unconditionally calls `runtime::spawn`: [7](#0-6) [8](#0-7) 

There is no global cap on concurrent spawned tasks. Critically, the `pending_delivered` entry is **never removed** after triggering a spawn — the attacker can re-trigger the same entry every second (once the `forward_rate_limiter` resets for that `(from, to)` pair).

**Resource exhaustion inside `try_nat_traversal`:**

Each spawned task runs up to 24 concurrent `try_nat_traversal` futures. Each future loops for 30 seconds, creating a new `TcpSocket` and calling `socket.connect()` every ~200ms with no semaphore or global socket limit: [9](#0-8) 

**Why existing checks fail:**

- `rate_limiter` (30/sec per session): limits message throughput but still allows 30 spawns/sec.
- `forward_rate_limiter` (1/sec per `(from, to, item_id)`): bypassed by using distinct spoofed `from` values.
- `HOLE_PUNCHING_INTERVAL` (2 min per `from`): bypassed by using distinct spoofed `from` values.
- `TIMEOUT` cleanup (5 min): entries persist long enough to be re-triggered many times.
- `pending_delivered` HashMap: unbounded, no size cap. [10](#0-9) 

## Impact Explanation

At steady state (30 seconds of attack at 30 `ConnectionRequest`/sec + 30 `ConnectionSync`/sec from a single session): 30 × 30 = 900 active spawned tasks, each running 24 concurrent `try_nat_traversal` loops. Each loop creates a `TcpSocket` every ~200ms → ~150 socket attempts per address over 30 seconds. This exhausts the OS file descriptor table (default Linux `ulimit -n`: 1024–65536) and TCP ephemeral port space, causing the node to fail to accept or initiate any legitimate connections — a **node crash/DoS**.

This matches the allowed impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

The attack requires only a single standard P2P connection to the victim. No privileged role, no proof-of-work, no key material, no Sybil capability. `ConnectionRequest` and `ConnectionSync` are standard production protocol messages. The spoofed `from` field is never authenticated against the session. The attack is fully reproducible from a single peer and can be sustained indefinitely by repeating the message sequence every 30 seconds.

## Recommendation

1. **Authenticate `from`**: In `ConnectionRequestProcess::execute`, verify that `content.from` matches the peer ID of the actual session (`self.peer`). Reject messages where `from` does not match the sender's authenticated peer ID.
2. **Cap `pending_delivered`**: Enforce a maximum size on the `pending_delivered` HashMap (e.g., 64 entries), evicting oldest entries when the cap is reached.
3. **Global concurrency limit on NAT traversal**: Use a `tokio::sync::Semaphore` to bound the total number of concurrent `try_nat_traversal` tasks globally (e.g., max 8–16).
4. **Remove entry after `ConnectionSync` consumption**: In `ConnectionSyncProcess::execute`, call `pending_delivered.remove(&content.from)` after consuming the entry to prevent repeated re-triggering.

## Proof of Concept

```
1. Attacker establishes a single TCP P2P session to the victim node.

2. For i in 0..30 (within 1 second, within rate_limiter budget):
     Send ConnectionRequest {
       from: random_peer_id_i,   // spoofed, distinct per message
       to: victim_peer_id,
       listen_addrs: [24 × "1.2.3.4:PORT_i_j"],  // valid TCP/IP4 addresses
       max_hops: 6,
       route: [],
     }
   → victim stores pending_delivered[random_peer_id_i] = ([24 addrs], now)
   → forward_rate_limiter passes because each (from_i, to, item_id) is distinct

3. For i in 0..30 (within 1 second):
     Send ConnectionSync {
       from: random_peer_id_i,
       to: victim_peer_id,
       route: [],
     }
   → victim finds pending_delivered[random_peer_id_i]
   → runtime::spawn fires with select_ok([24 × try_nat_traversal(...)])
   → entry is NOT removed; can be re-triggered after 1s

4. Each try_nat_traversal runs for 30s, creating a TcpSocket every ~200ms.
   After 30s: 900 active spawned tasks × 24 futures = 21,600 concurrent
   socket-creating loops.

5. Verify: victim's /proc/self/fd count approaches ulimit -n;
   legitimate connections are refused with EMFILE or ECONNREFUSED.
   Repeat step 3 every second to maintain pressure indefinitely.
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
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

**File:** network/src/protocols/hole_punching/mod.rs (L27-27)
```rust
const ADDRS_COUNT_LIMIT: usize = 24;
```

**File:** network/src/protocols/hole_punching/mod.rs (L44-46)
```rust
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
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
