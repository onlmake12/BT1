Audit Report

## Title
Unbounded `runtime::spawn` of NAT Traversal Tasks via `ConnectionSync` with Distinct `item_id`s — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

## Summary
The `forward_rate_limiter` and outer `rate_limiter` in the hole-punching protocol are both keyed with `item_id` as part of their composite key, meaning each distinct `item_id` value creates an independent rate-limit bucket. An attacker with a single P2P connection can first seed `pending_delivered` with one `ConnectionRequest`, then flood `ConnectionSync` messages using distinct `item_id`s to bypass both limiters and trigger an unbounded number of `runtime::spawn` calls. Each spawned task opens up to 24 concurrent TCP sockets and retries for 30 seconds, exhausting file descriptors, async task capacity, and CPU.

## Finding Description

**Step 1 — Seed `pending_delivered`**

The attacker sends a `ConnectionRequest` with `to = local_peer_id` and up to 24 valid TCP listen addresses. In `respond_delivered()`, the victim inserts the attacker's peer ID into `pending_delivered`: [1](#0-0) 

The only guard is a 2-minute re-insertion check, which does not prevent the initial insertion: [2](#0-1) 

The entry persists for `TIMEOUT = 5 minutes`: [3](#0-2) 

**Step 2 — Bypass both rate limiters with distinct `item_id`s**

The outer `rate_limiter` in `received()` is keyed by `(session_id, msg.item_id())`: [4](#0-3) 

The `forward_rate_limiter` in `ConnectionSyncProcess::execute()` is keyed by `(from, to, item_id)`: [5](#0-4) 

Both use `governor::RateLimiter` with `HashMapStateStore`, which creates a new independent bucket per unique key. With N distinct `item_id` values, N independent buckets are created, allowing N messages/second through the `forward_rate_limiter` (1/second per bucket) and up to 30N messages/second through the outer limiter (30/second per bucket). [6](#0-5) 

**Step 3 — Unconditional `runtime::spawn` per passing message**

Once both limiters pass and `pending_delivered` contains the attacker's `from` peer ID, `execute()` unconditionally calls `runtime::spawn` with no semaphore, counter, or cap on concurrent tasks: [7](#0-6) 

**Step 4 — Each task opens up to 24 TCP sockets for 30 seconds**

`ADDRS_COUNT_LIMIT = 24` addresses are stored in `pending_delivered` and passed as futures to `select_ok`: [8](#0-7) [9](#0-8) 

Each `try_nat_traversal` future retries TCP connections in a loop for up to 30 seconds, creating a new `TcpSocket` on every iteration: [10](#0-9) [11](#0-10) 

## Impact Explanation

In a 30-second window with N=1,000 distinct `item_id`s: 30,000 async tasks are spawned, each holding up to 24 open TCP sockets = up to 720,000 socket descriptors. This exhausts the OS file descriptor limit (typically 65,535 on Linux), the Tokio thread/task pool, and CPU from continuous TCP retry loops, causing the node to become unresponsive or crash. This matches the **High** impact: *Vulnerabilities which could easily crash a CKB node*.

## Likelihood Explanation

The attacker requires only a single standard P2P connection to the victim node — no special privilege, no proof-of-work, and no cryptographic material beyond a valid peer identity. The `ConnectionRequest` to seed `pending_delivered` is a normal protocol message. Sending thousands of `ConnectionSync` messages with distinct `item_id`s (cycling through `u32` values 0..N) is trivially scriptable. The attack is repeatable every 5 minutes when the `pending_delivered` entry expires, and can be re-seeded with another `ConnectionRequest`.

## Recommendation

1. **Fix the rate limiter key**: Key `forward_rate_limiter` by `(from, to)` only, removing `item_id` from the key, so the 1/second limit applies to the entire `(from, to)` pair regardless of `item_id`.
2. **Bound concurrent tasks per `(from, to)` pair**: Track active NAT traversal tasks per `(from, to)` pair (e.g., using a `HashSet` or `AtomicBool`) and skip spawning if a task is already running for that pair.
3. **Global task semaphore**: Introduce a bounded `tokio::sync::Semaphore` to cap the total number of concurrent NAT traversal tasks node-wide.

## Proof of Concept

```
1. Connect to victim node as peer A (attacker_peer_id).
2. Send ConnectionRequest{from=attacker_peer_id, to=victim_peer_id,
       listen_addrs=[<24 valid TCP addrs>]}.
   → victim inserts pending_delivered[attacker_peer_id] = ([addrs], now).
3. In a loop for i in 0..N (e.g., N=1000):
     Send ConnectionSync{from=attacker_peer_id, to=victim_peer_id,
                         route=[], item_id=i}.
     → passes outer rate_limiter (new bucket per (session_id, i))
     → passes forward_rate_limiter (new bucket per (from, to, i))
     → pending_delivered[attacker_peer_id] exists
     → runtime::spawn fires, creating a task with 24 try_nat_traversal futures
4. After 30 seconds: N*24 sockets open, N async tasks running.
   Assert: open fd count > system ulimit → node crashes or becomes unresponsive.
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

**File:** network/src/protocols/hole_punching/mod.rs (L28-28)
```rust
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L31-46)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;

/// Hole Punching Protocol
pub(crate) struct HolePunching {
    network_state: Arc<NetworkState>,
    bind_addr: Option<SocketAddr>,
    // Request timestamp recorded
    inflight_requests: HashMap<PeerId, u64>,
    // Delivered timestamp recorded
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L119-124)
```rust
                            let tasks = listens
                                .into_iter()
                                .map(|listen_addr| {
                                    Box::pin(try_nat_traversal(self.bind_addr, listen_addr))
                                })
                                .collect::<Vec<_>>();
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L143-163)
```rust
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

**File:** network/src/protocols/hole_punching/component/mod.rs (L80-84)
```rust
        let socket = create_socket(bind_addr, net_addr)?;

        match runtime::timeout(
            std::time::Duration::from_millis(200),
            socket.connect(net_addr),
```
