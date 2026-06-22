### Title
Attacker-Controlled `listen_addrs` in `ConnectionRequest` Enable SSRF-Like TCP Probing and Resource Exhaustion via `ConnectionSync` — (`network/src/protocols/hole_punching/component/`)

---

### Summary

An unprivileged, directly-connected P2P peer can cause the target CKB node to make outbound TCP connection attempts to arbitrary IP:port targets — including loopback (`127.0.0.1`) and RFC1918 addresses — by exploiting a two-step abuse of the hole-punching protocol. The attacker first populates `pending_delivered` with attacker-controlled addresses via a crafted `ConnectionRequest`, then triggers `try_nat_traversal` against those addresses via a crafted `ConnectionSync`. No authentication, PoW, or privileged role is required.

---

### Finding Description

**Step 1 — Populate `pending_delivered` with arbitrary addresses**

When the target node (T) receives a `ConnectionRequest` where `content.to == self_peer_id`, it calls `respond_delivered`. The `listen_addrs` field from the message is filtered only by transport type: [1](#0-0) 

The filter rejects Memory/Onion/Ws/Wss/Tls transports but **accepts any TCP address with an IPv4 or IPv6 component** — including `127.0.0.1`, `10.x.x.x`, `192.168.x.x`, etc. No private/loopback/RFC1918 filtering exists. The accepted addresses are then stored verbatim: [2](#0-1) 

The `from` field in the `ConnectionRequest` is taken directly from the message bytes with no check that it matches the actual sender's session peer ID: [3](#0-2) 

So the attacker can use any arbitrary `from` peer ID, bypassing the `HOLE_PUNCHING_INTERVAL` deduplication check (which only blocks re-use of the same `from` within 2 minutes) by rotating fresh peer IDs.

**Step 2 — Trigger `try_nat_traversal` via `ConnectionSync`**

When T receives a `ConnectionSync` where `content.to == self_peer_id` and `content.route` is empty, it looks up `pending_delivered[content.from]` and spawns `try_nat_traversal` tasks for every stored address: [4](#0-3) 

Again, `content.from` is attacker-controlled message bytes with no binding to the actual session peer ID. `try_nat_traversal` then makes repeated TCP `connect()` calls to the target address for up to **30 seconds** with retries every ~200ms: [5](#0-4) 

**Rate limiter analysis**

The session-level rate limiter allows **30 `ConnectionSync` messages per second** per `(session_id, msg_item_id)`: [6](#0-5) 

The `forward_rate_limiter` limits `(from, to, msg_item_id)` to 1/second, but the attacker bypasses this by using a different `from` peer ID for each `ConnectionRequest`/`ConnectionSync` pair. With 30 `ConnectionSync` messages/second per session, each spawning a 30-second task, a single attacker session accumulates up to **900 concurrent `try_nat_traversal` tasks**. Multiple attacker sessions multiply this further.

The `pending_delivered` map has no size cap and is only cleaned up every 5 minutes: [7](#0-6) 

---

### Impact Explanation

- **SSRF / internal network probing**: The node makes TCP `connect()` calls to attacker-specified addresses, including `127.0.0.1` (CKB RPC port 8114, local databases) and RFC1918 ranges. This can be used to probe internal services not exposed to the internet.
- **Resource exhaustion**: Hundreds to thousands of concurrent async tasks, each holding a `TcpSocket` and looping for 30 seconds, exhaust file descriptors, async task memory, and CPU on the victim node, degrading or crashing it.

---

### Likelihood Explanation

The attacker only needs a standard P2P connection to the target node — the normal operating condition for any CKB node. No special role, leaked key, or majority hashpower is required. The two-message sequence is trivially constructable using the public molecule-encoded wire format.

---

### Recommendation

1. **Filter private/loopback addresses** in `respond_delivered` before storing into `pending_delivered`. Reject addresses in `127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `::1`, and link-local ranges.
2. **Bind `content.from` to the actual session peer ID** in `ConnectionRequest` handling — reject messages where `content.from` does not match the authenticated peer ID of the sending session.
3. **Cap `pending_delivered` map size** to prevent unbounded accumulation.
4. **Limit concurrent `try_nat_traversal` tasks** with a semaphore or task counter.

---

### Proof of Concept

```
1. Attacker (peer A) establishes a P2P connection to target node T.

2. A sends ConnectionRequest:
     from = <random_peer_id>
     to   = <T's peer_id>
     listen_addrs = [/ip4/127.0.0.1/tcp/8114]
     route = []
     max_hops = 1

   → T calls respond_delivered(), filter passes 127.0.0.1 (TCP+IPv4),
     pending_delivered[random_peer_id] = ([/ip4/127.0.0.1/tcp/8114], now)

3. A sends ConnectionSync:
     from  = <same random_peer_id>
     to    = <T's peer_id>
     route = []

   → T: self_peer_id == content.to → true
   → T: pending_delivered.get(random_peer_id) → Some([/ip4/127.0.0.1/tcp/8114])
   → T: spawns try_nat_traversal(bind_addr, /ip4/127.0.0.1/tcp/8114)
   → T makes TCP connect() to 127.0.0.1:8114 repeatedly for 30 seconds.

4. Repeat steps 2–3 with fresh random_peer_id values at 30/sec to exhaust resources.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L196-215)
```rust
        let remote_listens: Vec<Multiaddr> = remote_listens
            .into_iter()
            .filter_map(|addr| match find_type(&addr) {
                TransportType::Memory
                | TransportType::Onion
                | TransportType::Ws
                | TransportType::Wss
                | TransportType::Tls => None,
                TransportType::Tcp => {
                    if addr
                        .iter()
                        .any(|p| matches!(p, Protocol::Ip4(_) | Protocol::Ip6(_)))
                    {
                        Some(addr)
                    } else {
                        None
                    }
                }
            })
            .collect();
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-124)
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

**File:** network/src/protocols/hole_punching/mod.rs (L172-175)
```rust
        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/mod.rs (L249-252)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (CHECK_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```
