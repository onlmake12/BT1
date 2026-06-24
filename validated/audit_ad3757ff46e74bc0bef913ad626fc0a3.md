Audit Report

## Title
HolePunching Protocol Accepts Arbitrary `listen_addrs` Without Private-IP Validation, Enabling Resource Exhaustion — (`network/src/protocols/hole_punching/component/connection_request.rs`, `network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

## Summary
The HolePunching protocol's address filtering in both `respond_delivered` and `try_nat_traversal` applies only a transport-type check (TCP + Ip4/Ip6), with no `is_reachable()` guard. Attacker-supplied private-range addresses pass through and are fed directly to `try_nat_traversal`, which opens real outbound TCP sockets retrying for up to 30 seconds each. Because the `forward_rate_limiter` is keyed on the message-level `from` field (not the session), an attacker can rotate spoofed `from` peer IDs to bypass it, while the session-level cap of 30 msg/s still allows spawning ~21,600 concurrent async futures, exhausting file descriptors and crashing the node's networking layer.

## Finding Description

**Path 1 — `connection_request.rs` lines 196–215 + `connection_sync.rs` lines 111–124:**

`respond_delivered` filters `remote_listens` only by transport type. Private-range and loopback addresses pass through and are stored verbatim in `pending_delivered`:

```rust
let remote_listens: Vec<Multiaddr> = remote_listens
    .into_iter()
    .filter_map(|addr| match find_type(&addr) {
        TransportType::Tcp => {
            if addr.iter().any(|p| matches!(p, Protocol::Ip4(_) | Protocol::Ip6(_))) {
                Some(addr)   // ← private IPs pass through
            } else { None }
        }
        _ => None,
    })
    .collect();
// ...
self.protocol.pending_delivered.insert(from_peer_id, (remote_listens, now));
``` [1](#0-0) 

When a subsequent `ConnectionSync` arrives, the stored addresses are consumed without any IP-class check and passed directly to `try_nat_traversal`: [2](#0-1) 

**Path 2 — `connection_request_delivered.rs` lines 237–257:**

When the victim is the `from` peer and receives `ConnectionRequestDelivered`, `try_nat_traversal` is called directly with `content.listen_addrs` after the same transport-type-only filter, with no `is_reachable()` check: [3](#0-2) 

**Actual TCP connection attempt — `component/mod.rs` lines 49–115:**

`try_nat_traversal` converts the `Multiaddr` to a `SocketAddr` and retries a real TCP `connect()` every ~200 ms for up to 30 seconds with no IP-class check: [4](#0-3) 

**Rate-limiter bypass:**

The session-level `rate_limiter` is keyed by `(session_id, msg.item_id())` and caps at 30 msg/s. The `forward_rate_limiter` is keyed by `(content.from, content.to, item_id)`, where `content.from` is taken from the message payload and is never verified against the actual session's peer ID. An attacker can rotate a fresh random `from` peer ID in each message to bypass the `forward_rate_limiter` entirely, while the session-level cap remains the only real constraint. [5](#0-4) [6](#0-5) 

**`pending_delivered` has no size bound:**

The `pending_delivered: HashMap<PeerId, PendingDeliveredInfo>` is cleaned only by the 5-minute `notify` timer. With rotating `from` IDs, an attacker inserts a new entry per message, growing the map unboundedly within each 5-minute window. [7](#0-6) 

**Contrast with guarded protocols:**

Discovery calls `is_reachable(socket_addr.ip())` before storing any address: [8](#0-7) 

Identify applies the same guard in `process_listens`: [9](#0-8) 

The HolePunching code contains zero uses of `is_reachable`.

**Resource exhaustion arithmetic:**
- Session rate cap: 30 `ConnectionRequest` + 30 `ConnectionSync` per second
- Each `ConnectionSync` triggers `select_ok` over up to 24 concurrent `try_nat_traversal` futures
- Each future runs for up to 30 seconds, calling `create_socket` every ~200 ms
- Steady-state concurrent futures: 30 × 24 × 30 = **21,600**
- Socket allocations per second: ~150 per future × 21,600 = **~108,000/s**, exhausting the process file-descriptor table

## Impact Explanation

**High (10001–15000 points): Crash a CKB node.** File-descriptor exhaustion causes all subsequent network I/O — including consensus-critical peer connections — to fail with `EMFILE`/`ENFILE`, effectively crashing the node's networking layer. A single malicious connected peer can sustain this attack indefinitely. The HolePunching protocol is enabled in the default configuration: [10](#0-9) 

## Likelihood Explanation

Any peer that completes a normal P2P handshake can send `ConnectionRequest` and `ConnectionSync` messages. No special privilege or stake is required. The victim's peer ID is public. The attacker needs only one established session and can sustain the attack continuously by rotating spoofed `from` peer IDs to bypass the `forward_rate_limiter`. The attack is fully repeatable and requires no victim interaction beyond the initial connection.

## Recommendation

Apply `is_reachable()` to every address extracted from `listen_addrs` in both `respond_delivered` (in `connection_request.rs`) and `try_nat_traversal` (in `connection_request_delivered.rs`) before storing or dialing them, mirroring the guard already present in the Discovery and Identify protocols:

```rust
use p2p::utils::{is_reachable, multiaddr_to_socketaddr};

.filter(|addr| match multiaddr_to_socketaddr(addr) {
    Some(socket_addr) => is_reachable(socket_addr.ip()),
    None => false,
})
```

Additionally, bound the number of concurrently active `try_nat_traversal` tasks globally using a semaphore or task counter, add a size cap on `pending_delivered`, and verify that `content.from` in `ConnectionRequest` matches the actual session's peer ID.

## Proof of Concept

1. Connect to a target CKB node as a normal peer (standard P2P handshake).
2. In a loop at ~30 iterations/second, send `HolePunchingMessage::ConnectionRequest` with:
   - `from` = a freshly generated random peer ID (different each iteration to bypass `forward_rate_limiter`)
   - `to` = the victim's own peer ID (so the victim processes it as the `to` target)
   - `listen_addrs` = 24 addresses in RFC-1918 ranges (e.g., `/ip4/192.168.1.1/tcp/22` through `/ip4/192.168.1.24/tcp/22`)
   - `max_hops` = 1
3. For each sent `ConnectionRequest`, immediately send a matching `ConnectionSync` with `from` = the same random peer ID used in step 2 and `to` = victim's peer ID.
4. The victim's `respond_delivered` stores 24 private-IP addresses in `pending_delivered` (no `is_reachable` check), and the subsequent `ConnectionSync` triggers `select_ok` over 24 concurrent `try_nat_traversal` futures per message.
5. After 30 seconds of sustained sending, the victim node has ~21,600 concurrent async futures each holding TCP sockets. Monitor the victim's file-descriptor count (`/proc/<pid>/fd`) to observe exhaustion; the node's P2P layer will begin failing all new connections with `EMFILE`, effectively crashing its networking.

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L196-237)
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

        if remote_listens.is_empty() {
            return StatusCode::Ignore.with_context("remote listen address is empty");
        }

        debug!(
            "current peer is the target peer {}, send a response back",
            to_peer_id
        );

        if let Err(error) = self
            .p2p_control
            .send_message_to(self.peer, proto_id, new_message)
            .await
        {
            return StatusCode::ForwardError.with_context(error);
        }

        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-128)
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
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L237-257)
```rust
    fn try_nat_traversal(&self, ttl: u64, remote_addrs: Vec<Multiaddr>) {
        let tasks = remote_addrs
            .into_iter()
            .filter_map(|listen_addr| match find_type(&listen_addr) {
                TransportType::Tcp => {
                    if listen_addr
                        .iter()
                        .any(|p| matches!(p, Protocol::Ip4(_) | Protocol::Ip6(_)))
                    {
                        Some(Box::pin(try_nat_traversal(self.bind_addr, listen_addr)))
                    } else {
                        None
                    }
                }
                TransportType::Memory
                | TransportType::Onion
                | TransportType::Ws
                | TransportType::Wss
                | TransportType::Tls => None,
            })
            .collect::<Vec<_>>();
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L49-115)
```rust
pub(crate) async fn try_nat_traversal(
    bind_addr: Option<SocketAddr>,
    addr: Multiaddr,
) -> Result<(TcpStream, Multiaddr), std::io::Error> {
    let net_addr = match multiaddr_to_socketaddr(&addr) {
        Some(addr) => addr,
        None => {
            debug!("Failed to convert multiaddr to socketaddr");
            return Err(std::io::ErrorKind::InvalidInput.into());
        }
    };

    // Use a fixed interval but add a small amount of randomness
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

**File:** network/src/protocols/hole_punching/mod.rs (L169-175)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
        let status = self.network_state.connection_status();

        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/mod.rs (L249-257)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (CHECK_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/discovery/mod.rs (L332-341)
```rust
    fn is_valid_addr(&self, addr: &Multiaddr) -> bool {
        if !self.discovery_local_address {
            match multiaddr_to_socketaddr(addr) {
                Some(socket_addr) => is_reachable(socket_addr.ip()),
                None => true,
            }
        } else {
            true
        }
    }
```

**File:** network/src/protocols/identify/mod.rs (L138-145)
```rust
            let global_ip_only = self.global_ip_only;
            let reachable_addrs = listens
                .into_iter()
                .filter(|addr| match multiaddr_to_socketaddr(addr) {
                    Some(socket_addr) => !global_ip_only || is_reachable(socket_addr.ip()),
                    None => true,
                })
                .collect::<Vec<_>>();
```

**File:** network/src/network.rs (L941-953)
```rust
        #[cfg(not(target_family = "wasm"))]
        if config
            .support_protocols
            .contains(&SupportProtocol::HolePunching)
        {
            let hole_punching_state = Arc::clone(&network_state);
            let hole_punching_meta =
                SupportProtocols::HolePunching.build_meta_with_service_handle(move || {
                    ProtocolHandle::Callback(Box::new(
                        crate::protocols::hole_punching::HolePunching::new(hole_punching_state),
                    ))
                });
            protocol_metas.push(hole_punching_meta);
```
