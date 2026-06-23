### Title
Duplicate `listen_addrs` in `ConnectionRequestDelivered` Causes 24x Redundant NAT Traversal TCP Tasks — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

### Summary

`DeliverdContent::try_from` and `try_nat_traversal` perform no deduplication of `listen_addrs`. A peer acting as the hole-punching target can respond with up to `ADDRS_COUNT_LIMIT = 24` identical TCP addresses, causing the victim node to spawn 24 concurrent `try_nat_traversal` async tasks all connecting to the same endpoint, each retrying for up to 30 seconds.

### Finding Description

`ADDRS_COUNT_LIMIT` is defined as 24. [1](#0-0) 

In `DeliverdContent::try_from`, `listen_addrs` are parsed and peer-ID-validated but never deduplicated: [2](#0-1) 

In `execute()`, the only guard is a count check (`> ADDRS_COUNT_LIMIT`), which allows 24 identical entries through: [3](#0-2) 

`try_nat_traversal` iterates the raw (undeduped) list and creates one pinned future per entry, all passed to `select_ok`: [4](#0-3) 

Each spawned `try_nat_traversal` future runs a retry loop for up to 30 seconds, allocating a new `TcpSocket` on every iteration (~150 retries at ~200 ms intervals): [5](#0-4) 

### Impact Explanation

With 24 identical addresses, the victim spawns 24 concurrent async tasks instead of 1, each independently retrying TCP connections to the same endpoint for 30 seconds. This is a 24× amplification of TCP socket allocations (~3 600 socket operations vs ~150). The impact is bounded and async (tokio tasks, not OS threads), so it is resource waste rather than exhaustion.

### Likelihood Explanation

The attack requires the victim to have first initiated a hole-punching `ConnectionRequest` to the attacker's peer ID (the `inflight_requests.remove(&content.to)` guard at line 160 enforces this). [6](#0-5) 

Once the victim has sent a `ConnectionRequest` to the attacker, the attacker knows both the victim's peer ID (from the request) and their own peer ID, and can craft the malicious `ConnectionRequestDelivered` reply. The `inflight_requests.remove` means each inflight slot can only be exploited once, limiting repeated amplification. The `forward_rate_limiter` and session-level `rate_limiter` provide additional throttling. [7](#0-6) 

### Recommendation

Deduplicate `listen_addrs` before building the task list, either in `DeliverdContent::try_from` (using a `HashSet<Multiaddr>`) or at the start of `try_nat_traversal` in `connection_request_delivered.rs`. The same fix should be applied to the analogous path in `connection_sync.rs`.

### Proof of Concept

1. Victim node has hole punching enabled and sends a `ConnectionRequest` with `to = attacker_peer_id`.
2. Attacker crafts a `ConnectionRequestDelivered` with `from = victim_peer_id`, `to = attacker_peer_id`, and `listen_addrs` = 24 copies of the same valid TCP `Multiaddr` (e.g., `/ip4/1.2.3.4/tcp/4000/p2p/<attacker_peer_id>`).
3. Victim receives the message; all checks pass (peer ID valid, count = 24 ≤ 24, rate limit not yet hit).
4. `try_nat_traversal` builds 24 identical futures and passes them to `select_ok`.
5. 24 concurrent async tasks are spawned, each creating TCP sockets to `1.2.3.4:4000` for 30 seconds — 24× the intended resource usage.

### Citations

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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L51-71)
```rust
        let listen_addrs = value
            .listen_addrs()
            .iter()
            .map(
                |raw| match Multiaddr::try_from(raw.bytes().raw_data().to_vec()) {
                    Ok(mut addr) => {
                        if let Some(peer_id) = extract_peer_id(&addr) {
                            if peer_id != to {
                                return Err(StatusCode::InvalidListenAddrLen
                                    .with_context("peer id in listen address is invalid"));
                            }
                        } else {
                            addr.push(Protocol::P2P(Cow::Borrowed(to.as_bytes())));
                        }
                        Ok(addr)
                    }
                    Err(_) => Err(StatusCode::InvalidListenAddrLen
                        .with_context("the listen address is invalid")),
                },
            )
            .collect::<Result<Vec<_>, _>>()?;
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L125-128)
```rust
        if content.listen_addrs.len() > ADDRS_COUNT_LIMIT || content.listen_addrs.is_empty() {
            return StatusCode::InvalidListenAddrLen
                .with_context("the listen address count is too large or empty");
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L160-176)
```rust
                    let request_start = self.protocol.inflight_requests.remove(&content.to);

                    match request_start {
                        Some(start) => {
                            let res = self.respond_sync(content.from).await;
                            if !res.is_ok() {
                                return res;
                            }
                            let now = unix_time_as_millis();
                            let ttl = now - start;

                            self.try_nat_traversal(ttl, content.listen_addrs);

                            Status::ok()
                        }
                        None => StatusCode::Ignore.with_context("the request is not in flight"),
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
