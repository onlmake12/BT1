Audit Report

## Title
Unauthenticated SSRF and Resource Exhaustion via Hole-Punching Protocol: Attacker-Controlled `listen_addrs` Cause Victim to Make Outbound TCP Connections to Arbitrary Hosts — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary
An unprivileged peer with a single P2P connection can cause a CKB node to make outbound TCP connections to arbitrary IP:port combinations — including loopback and RFC-1918 addresses — by sending crafted `ConnectionRequest` messages with attacker-controlled `listen_addrs`. A subsequent `ConnectionSync` triggers `try_nat_traversal`, which retries TCP connections every ~200ms for 30 seconds per address. By rotating fake `from` peer IDs to bypass per-key rate limits, an attacker can spawn ~21,600 concurrent socket tasks, exhausting file descriptors and crashing the node.

## Finding Description

**Root cause 1 — No IP-range validation in `respond_delivered`:**

The address filter in `respond_delivered` only rejects non-TCP transports. Any TCP address with an `Ip4` or `Ip6` component passes unconditionally, including `127.0.0.1`, `10.x.x.x`, `192.168.x.x`, and `::1`: [1](#0-0) 

The filtered addresses are then stored in `pending_delivered` without any IP-range check: [2](#0-1) 

**Root cause 2 — `content.from` is never validated against the actual session peer ID:**

`ConnectionRequestProcess` holds a `peer: PeerIndex` field identifying the actual connected session, but `execute()` never checks that `content.from` matches the peer ID of `self.peer` before calling `respond_delivered`: [3](#0-2) [4](#0-3) 

The `HOLE_PUNCHING_INTERVAL` cooldown is keyed by `from_peer_id` (attacker-controlled), so rotating fake `from` values trivially bypasses it: [5](#0-4) 

**Root cause 3 — `ConnectionSyncProcess` has no `peer` field and cannot authenticate the sender:**

The struct carries no session identity: [6](#0-5) 

When `content.to == self_peer_id`, it unconditionally retrieves `pending_delivered[content.from]` and spawns `select_ok` over all 24 `try_nat_traversal` futures within a single `runtime::spawn`: [7](#0-6) [8](#0-7) 

**Root cause 4 — `try_nat_traversal` opens real TCP sockets for up to 30 seconds:**

Each future loops for 30 seconds, creating a new `TcpSocket` and calling `socket.connect(net_addr)` every ~200ms: [9](#0-8) 

**Why existing guards are insufficient:**

- The session `rate_limiter` (keyed by `(session_id, msg.item_id())`) allows 30 messages/second per session — this is the attacker's throughput budget, not a defense: [10](#0-9) 
- The `forward_rate_limiter` (keyed by `(from, to, msg_item_id)`) allows 1 req/sec per tuple, but is trivially bypassed by rotating `from` peer IDs: [11](#0-10) 
- `pending_delivered` is an unbounded `HashMap` cleaned up only every 5 minutes: [12](#0-11) 
- `ADDRS_COUNT_LIMIT` is 24, setting the per-`ConnectionSync` socket fan-out: [13](#0-12) 

## Impact Explanation

**SSRF:** The victim node makes real TCP connections to attacker-specified internal addresses. If a connection succeeds, `control.raw_session(stream, addr, RawSessionInfo::inbound(listen_addr))` is called, opening a raw P2P session to that internal service. [14](#0-13) 

**Resource exhaustion / node crash (High):** With 30 `ConnectionRequest`/`ConnectionSync` pairs per second (session rate limit), each carrying 24 addresses, the attacker spawns 30 tasks/sec, each running 24 concurrent `try_nat_traversal` futures for 30 seconds. After 30 seconds: 30 tasks/sec × 30 sec = 900 concurrent spawned tasks × 24 concurrent socket futures = 21,600 concurrent `TcpSocket` file descriptors. This exhausts the OS file-descriptor table and crashes the node.

This maps to **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

The attacker requires only a single standard P2P connection — no special privileges, leaked keys, or majority hashpower. The two-message sequence (`ConnectionRequest` then `ConnectionSync`) is constructable from the published molecule schema. Rotating `from` peer IDs requires no external state. Any publicly reachable CKB node is a valid target.

## Recommendation

1. **Validate `from` against the actual session peer ID:** In `ConnectionRequestProcess::execute`, when `self_peer_id == content.to`, reject messages where `content.from` does not match the peer ID of `self.peer`.
2. **Filter private/loopback addresses in `respond_delivered`:** Reject any `listen_addr` whose IP component is loopback (`127.0.0.0/8`, `::1`), link-local (`169.254.0.0/16`, `fe80::/10`), or RFC-1918 private (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`).
3. **Add a `peer` field to `ConnectionSyncProcess` and authenticate the sender:** Verify that the session sending the `ConnectionSync` is the peer identified by `content.from` before acting on `pending_delivered`.
4. **Cap `pending_delivered` size:** Enforce a maximum number of entries (e.g., 1 per connected peer) to prevent unbounded memory growth.

## Proof of Concept

```
1. Attacker (peer_id=A) establishes a standard P2P connection to victim (peer_id=V).

2. Attacker sends (30 times/sec, each with a fresh random from=Ai):
   ConnectionRequest {
     from: Ai,
     to: V,
     max_hops: 0,
     route: [],
     listen_addrs: [/ip4/127.0.0.1/tcp/6379, ..., /ip4/192.168.1.1/tcp/22]  // 24 addrs
   }

3. Victim's respond_delivered():
   - TCP/IPv4 filter passes all 24 addresses (lines 196–215 of connection_request.rs)
   - Stores pending_delivered[Ai] = ([...24 addrs...], now)
   - forward_rate_limiter passes because (Ai, V, item_id) is fresh each time

4. Attacker sends (30 times/sec, matching from=Ai):
   ConnectionSync { from: Ai, to: V, route: [] }

5. Victim's ConnectionSyncProcess::execute():
   - self_peer_id == content.to → passive branch
   - Retrieves pending_delivered[Ai] → 24 addresses
   - Spawns runtime::spawn with select_ok over 24 try_nat_traversal futures,
     each retrying TCP connect every ~200ms for 30s

6. After 30 seconds:
   30 spawned tasks/sec × 30 sec = 900 concurrent tasks
   × 24 concurrent socket futures = 21,600 concurrent TcpSocket FDs
   → file descriptor exhaustion → node crash

   Simultaneously, internal services at the specified addresses receive TCP SYN
   packets from the victim every ~200ms for 30 seconds (SSRF / internal port scan).
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L85-91)
```rust
pub(crate) struct ConnectionRequestProcess<'a> {
    message: packed::ConnectionRequestReader<'a>,
    protocol: &'a mut HolePunching,
    peer: PeerIndex,
    p2p_control: &'a ServiceAsyncControl,
    msg_item_id: u32,
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L51-57)
```rust
pub(crate) struct ConnectionSyncProcess<'a> {
    message: packed::ConnectionSyncReader<'a>,
    protocol: &'a HolePunching,
    p2p_control: &'a ServiceAsyncControl,
    bind_addr: Option<SocketAddr>,
    msg_item_id: u32,
}
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

**File:** network/src/protocols/hole_punching/component/mod.rs (L65-110)
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
```

**File:** network/src/protocols/hole_punching/mod.rs (L27-27)
```rust
const ADDRS_COUNT_LIMIT: usize = 24;
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

**File:** network/src/protocols/hole_punching/mod.rs (L173-174)
```rust
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```
