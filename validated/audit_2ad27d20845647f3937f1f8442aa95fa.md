### Title
`pending_delivered` Entry Not Consumed After `ConnectionSync` Processing Allows Repeated NAT Traversal Triggering - (File: `network/src/protocols/hole_punching/component/connection_sync.rs`)

---

### Summary

In the CKB hole-punching protocol, when the target (`to`) node processes a `ConnectionSync` message and spawns a NAT traversal task, the `pending_delivered` map entry for `content.from` is **read but never removed**. This is directly analogous to the Popcorn `lastHarvest` bug: a state variable that gates a repeated action is initialized on first use but never consumed/updated when the action executes, allowing the action to be triggered on every subsequent invocation up to the rate limit.

---

### Finding Description

`HolePunching` maintains `pending_delivered: HashMap<PeerId, (Vec<Multiaddr>, u64)>`, which stores the remote listen addresses and a timestamp for each `from` peer that has been responded to. [1](#0-0) 

This map serves two roles:

**Role 1 — Cooldown gate in `respond_delivered`** (`connection_request.rs` lines 161–167): prevents re-responding to the same `from` peer within `HOLE_PUNCHING_INTERVAL` (2 minutes). The timestamp **is** updated here after the action executes. [2](#0-1) [3](#0-2) 

**Role 2 — Permission gate in `ConnectionSyncProcess::execute`** (`connection_sync.rs` lines 111–115): the listen addresses are read to spawn NAT traversal tasks. The entry is **never removed or cleared** after use. [4](#0-3) 

The entry only expires via the periodic `notify()` cleanup after `TIMEOUT` = 5 minutes: [5](#0-4) 

The only protection against repeated triggering is the `forward_rate_limiter`, configured at 1 request/second per `(from, to, msg_item_id)` key: [6](#0-5) [7](#0-6) 

---

### Impact Explanation

An unprivileged peer connected to the target node can:

1. Send a `ConnectionRequest` with `from = attacker_peer_id`, `to = target_peer_id`.
2. The target node stores `pending_delivered[attacker_peer_id] = (listen_addrs, now)` and sends `ConnectionRequestDelivered`.
3. The attacker then sends `ConnectionSync` messages at the maximum rate of 1/second for the full 5-minute `TIMEOUT` window — up to **300 messages**.
4. Each message passes the rate limiter and triggers a new `runtime::spawn` of `try_nat_traversal`, which runs for up to **30 seconds** making TCP connection attempts every ~200 ms.

At peak, ~30 concurrent NAT traversal tasks are active per `(from, to)` pair, each making ~150 TCP connection attempts — totalling ~4,500 outbound TCP attempts per `(from, to)` pair per 5-minute window. This exhausts file descriptors, CPU, and outbound connection capacity on the victim node. [8](#0-7) [9](#0-8) 

---

### Likelihood Explanation

The attack requires only a direct P2P connection to the target node and the ability to send `HolePunching` protocol messages — both available to any unprivileged peer. No special privileges, keys, or majority hashpower are needed. The `ConnectionRequest` step is trivially satisfied by any connected peer. The attack is repeatable every 5 minutes as the `pending_delivered` entry expires and can be re-created.

---

### Recommendation

After `ConnectionSync` is processed and NAT traversal is spawned, clear the listen addresses from the `pending_delivered` entry to prevent repeated triggering while preserving the timestamp for the `respond_delivered` cooldown:

```rust
// In connection_sync.rs, after spawning the NAT traversal task:
if let Some(entry) = self.protocol.pending_delivered.get_mut(&content.from) {
    entry.0.clear(); // Consume the listen addresses; keep timestamp for cooldown
}
```

Alternatively, use a separate `HashSet<PeerId>` to track peers for which NAT traversal has already been started within the current session, and skip re-triggering if the peer is already present.

---

### Proof of Concept

```
Attacker (peer A) → connects to Target (peer T)

1. A sends: ConnectionRequest { from=A, to=T, listen_addrs=[...], max_hops=6 }
   T processes: respond_delivered(A, T, [...])
     → pending_delivered[A] = ([listen_addrs], now)
     → sends ConnectionRequestDelivered back to A

2. A sends: ConnectionSync { from=A, to=T, route=[] }  ← 1 per second
   T processes: ConnectionSyncProcess::execute()
     → forward_rate_limiter.check_key((A, T, ConnectionSync_id)) → OK
     → pending_delivered.get(A) → Some([listen_addrs])   ← entry NOT removed
     → runtime::spawn(try_nat_traversal(...))             ← new task spawned
     → returns Status::ok()

3. Repeat step 2 every second for 5 minutes → 300 NAT traversal tasks spawned
   Each task: 30s timeout, TCP connect every ~200ms → ~150 TCP attempts/task
   Peak concurrent tasks: ~30 → ~4,500 TCP attempts in flight
``` [10](#0-9) [11](#0-10)

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L24-28)
```rust
pub(crate) const HOLE_PUNCHING_INTERVAL: u64 = 2 * 60 * 1000; // 2 minutes
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L38-47)
```rust
pub(crate) struct HolePunching {
    network_state: Arc<NetworkState>,
    bind_addr: Option<SocketAddr>,
    // Request timestamp recorded
    inflight_requests: HashMap<PeerId, u64>,
    // Delivered timestamp recorded
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
}
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L76-173)
```rust
    pub(crate) async fn execute(self) -> Status {
        let content = match SyncContent::try_from(&self.message) {
            Ok(content) => content,
            Err(status) => return status,
        };

        if content.route.len() > MAX_HOPS as usize {
            return StatusCode::InvalidRoute.with_context("the route length is too long");
        }
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

        match content.route.last() {
            Some(next_peer_id) => self.forward_sync(next_peer_id).await,
            None => {
                let self_peer_id = self.protocol.network_state.local_peer_id();
                if self_peer_id != &content.to {
                    // forward the message to the `to` peer
                    self.forward_sync(&content.to).await
                } else {
                    // Current node should be the `to` target.
                    if let Some(metrics) = ckb_metrics::handle() {
                        metrics.ckb_hole_punching_passive_count.inc();
                    }

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
                                }
                                None => {
                                    StatusCode::Ignore.with_context("no listen address configured")
                                }
                            }
                        }
                        None => StatusCode::Ignore
                            .with_context("the from peer id is not in the pending list"),
                    }
                }
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L62-114)
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
```
