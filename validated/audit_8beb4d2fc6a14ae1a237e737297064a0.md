### Title
Unauthenticated `from==to` Self-Referential Hole-Punching Triggers Unbounded NAT Traversal Task Spawning — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

---

### Summary

An unprivileged remote peer can cause a victim CKB node to spawn long-running background NAT traversal tasks targeting attacker-controlled addresses by sending two crafted hole-punching messages: a `ConnectionRequest` with `from == to == victim_peer_id` (to seed `pending_delivered`), followed by a `ConnectionSync` with the same self-referential fields. Neither message type validates that `from != to` or that `from` matches the actual sender's peer ID.

---

### Finding Description

**Phase 1 — Seed `pending_delivered[victim_peer_id]`**

The attacker sends a `ConnectionRequest` with `from = victim_peer_id`, `to = victim_peer_id`, `listen_addrs = [attacker_tcp_addr]`, `route = []`.

In `ConnectionRequestProcess::execute()`:
- The only identity check is `content.route.contains(self_peer_id)` (line 128), which passes because `route` is empty.
- Since `self_peer_id == &content.to`, `respond_delivered(victim_peer_id, victim_peer_id, [attacker_tcp_addr])` is called.
- After sending a `ConnectionRequestDelivered` back to the attacker's session, the map is updated:

```rust
self.protocol.pending_delivered.insert(from_peer_id, (remote_listens, now));
// from_peer_id == victim_peer_id, remote_listens == [attacker_tcp_addr]
``` [1](#0-0) [2](#0-1) [3](#0-2) 

**Phase 2 — Trigger NAT traversal via `ConnectionSync`**

The attacker sends a `ConnectionSync` with `from = victim_peer_id`, `to = victim_peer_id`, `route = []`.

In `ConnectionSyncProcess::execute()`:
- `route.last()` is `None`, so the routing branch is skipped.
- `self_peer_id == &content.to` → enters the "target" branch.
- `pending_delivered.get(&content.from)` resolves to `pending_delivered.get(&victim_peer_id)` → `Some([attacker_tcp_addr])`.
- A `runtime::spawn` task is launched that runs `try_nat_traversal` for 30 seconds. [4](#0-3) [5](#0-4) 

**`try_nat_traversal` behavior:**

The spawned task loops for 30 seconds, creating a new `TcpSocket` every ~200ms, binding it to the victim's listen address with `SO_REUSEADDR`/`SO_REUSEPORT`, and attempting to connect to the attacker-controlled address. [6](#0-5) [7](#0-6) 

---

### Impact Explanation

- Each spawned task runs for 30 seconds, making ~150 TCP connection attempts, each creating a socket bound to the victim's listen port with `SO_REUSEPORT`.
- The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)`. With `from == to == victim_peer_id`, the key is constant, allowing 1 trigger per second.
- After 30 seconds: 30 concurrent tasks × ~150 attempts = ~4,500 active TCP connection attempts, consuming file descriptors, CPU, and memory.
- The `SO_REUSEPORT` binding from the victim's own listen port for each outbound socket can interfere with the listening socket's accept queue on Linux.
- The attacker can sustain this indefinitely from a single connection. [8](#0-7) [9](#0-8) 

---

### Likelihood Explanation

- Requires only a standard P2P connection — no privileges, no keys, no hashpower.
- The victim's peer ID is publicly known (it is exchanged during the P2P handshake).
- The two-message sequence is straightforward to craft.
- The `HOLE_PUNCHING_INTERVAL` (2 minutes) rate-limits re-seeding `pending_delivered`, but once seeded, the `ConnectionSync` trigger is limited only to 1/second by `forward_rate_limiter`, which is sufficient to accumulate tasks. [10](#0-9) [11](#0-10) 

---

### Recommendation

1. **Reject `from == to`** in both `ConnectionRequest` and `ConnectionSync` parsing/validation — add an explicit check and return a ban-worthy status code (4xx).
2. **Validate that `from` matches the actual sender's peer ID** in `ConnectionRequest` (the session's authenticated peer ID is available via the peer registry).
3. **Reject `from == self_peer_id`** in `ConnectionSync::execute()` before looking up `pending_delivered`. [12](#0-11) [13](#0-12) 

---

### Proof of Concept

```
1. Attacker connects to victim via P2P (learns victim_peer_id from handshake).

2. Attacker sends ConnectionRequest:
     from        = victim_peer_id
     to          = victim_peer_id
     listen_addrs = [attacker_ip:attacker_port]  // valid TCP addr
     route       = []
     max_hops    = 6

   → Victim: self_peer_id == content.to → respond_delivered()
   → pending_delivered[victim_peer_id] = ([attacker_ip:attacker_port], now)

3. Attacker sends ConnectionSync (once per second):
     from  = victim_peer_id
     to    = victim_peer_id
     route = []

   → Victim: self_peer_id == content.to
   → pending_delivered.get(&victim_peer_id) = Some([attacker_ip:attacker_port])
   → runtime::spawn(try_nat_traversal(bind_addr, attacker_ip:attacker_port))

4. After 30 seconds: 30 concurrent tasks running, each making ~150 TCP
   connection attempts from victim's listen port (SO_REUSEPORT) to attacker.
   File descriptor exhaustion and CPU load accumulate on victim.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L110-153)
```rust
    pub(crate) async fn execute(mut self) -> Status {
        let content = match RequestContent::try_from(&self.message) {
            Ok(content) => content,
            Err(status) => return status,
        };
        if content.listen_addrs.len() > ADDRS_COUNT_LIMIT || content.listen_addrs.is_empty() {
            return StatusCode::InvalidListenAddrLen
                .with_context("the listen address count is too large or empty");
        }

        if content.max_hops > MAX_HOPS {
            return StatusCode::InvalidMaxTTL.into();
        }
        if content.route.len() > MAX_HOPS as usize {
            return StatusCode::InvalidRoute.with_context("the route length is too long");
        }

        let self_peer_id = self.protocol.network_state.local_peer_id();
        if content.route.contains(self_peer_id) {
            return StatusCode::Ignore.with_context("the message is passed, ignore it");
        }

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

        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
        } else if content.max_hops == 0u8 {
            StatusCode::ReachedMaxHops.into()
        } else {
            self.forward_message(self_peer_id, &content.to).await
        }
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L76-96)
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
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L98-115)
```rust
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
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L117-163)
```rust
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

**File:** network/src/protocols/hole_punching/component/mod.rs (L62-115)
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
}
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L117-148)
```rust
fn create_socket(
    bind_addr: Option<SocketAddr>,
    target_addr: SocketAddr,
) -> Result<TcpSocket, std::io::Error> {
    let socket = match bind_addr {
        Some(listen_addr) => match (listen_addr.ip(), target_addr.ip()) {
            (IpAddr::V4(_), IpAddr::V4(_)) => {
                let socket = TcpSocket::new_v4()?;
                socket.set_reuseaddr(true)?;
                #[cfg(all(unix, not(target_os = "solaris"), not(target_os = "illumos")))]
                socket.set_reuseport(true)?;
                socket.bind(listen_addr)?;
                socket
            }
            (IpAddr::V6(_), IpAddr::V6(_)) => {
                let socket = TcpSocket::new_v6()?;
                socket.set_reuseaddr(true)?;
                #[cfg(all(unix, not(target_os = "solaris"), not(target_os = "illumos")))]
                socket.set_reuseport(true)?;
                socket.bind(listen_addr)?;
                socket
            }
            (IpAddr::V4(_), IpAddr::V6(_)) => TcpSocket::new_v6()?,
            (IpAddr::V6(_), IpAddr::V4(_)) => TcpSocket::new_v4()?,
        },
        None => match target_addr.ip() {
            IpAddr::V4(_) => TcpSocket::new_v4()?,
            IpAddr::V6(_) => TcpSocket::new_v6()?,
        },
    };
    Ok(socket)
}
```

**File:** network/src/protocols/hole_punching/mod.rs (L24-24)
```rust
pub(crate) const HOLE_PUNCHING_INTERVAL: u64 = 2 * 60 * 1000; // 2 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L44-46)
```rust
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```
