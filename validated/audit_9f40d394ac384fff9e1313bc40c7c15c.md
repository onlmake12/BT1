The code is fully readable. Let me trace the exact attack path through all three files.

**Key observations:**

**1. No validation that `content.from` matches the actual sender's peer ID**

In `connection_request.rs` lines 36–38, `from` is parsed directly from the message bytes with no check against the actual session's peer ID. An attacker can put any peer ID in `from`. [1](#0-0) 

**2. `respond_delivered` stores attacker-controlled addresses without IP range validation**

The filter at lines 196–215 only removes non-TCP transports (Memory, Onion, Ws, Wss, Tls). Any TCP address with an IPv4 or IPv6 component passes — including `127.0.0.1`, `10.x.x.x`, `192.168.x.x`, etc. [2](#0-1) 

The filtered addresses are then stored unconditionally: [3](#0-2) 

**3. `ConnectionSyncProcess::execute()` retrieves stored addresses and calls `try_nat_traversal` with no further validation**

When the victim receives a `ConnectionSync(from=attacker_id, to=victim_id)` with empty route, it looks up `pending_delivered.get(&content.from)` and spawns TCP connection tasks to every stored address: [4](#0-3) 

**4. `try_nat_traversal` makes real outbound TCP connections for up to 30 seconds**

It retries every ~200ms for 30 seconds to the attacker-specified address: [5](#0-4) 

**5. No check that the `ConnectionSync` sender is the same peer as the `from` field**

`ConnectionSyncProcess` does not verify that the peer sending the sync message is actually the peer identified by `content.from`. Any connected peer can trigger NAT traversal to any address stored in `pending_delivered`. [6](#0-5) 

---

### Title
Unauthenticated SSRF via Hole-Punching Protocol: Attacker-Controlled `listen_addrs` Cause Victim to Make Outbound TCP Connections to Arbitrary Hosts — (`network/src/protocols/hole_punching/component/connection_request.rs`)

### Summary
An unprivileged peer connected to a CKB node can cause that node to make outbound TCP connections to arbitrary IP:port combinations — including internal network addresses — by sending a crafted `ConnectionRequest` followed by a `ConnectionSync` message. No authentication, PoW, or privileged role is required.

### Finding Description
The hole-punching protocol's `respond_delivered` function accepts attacker-supplied `listen_addrs` from a `ConnectionRequest` message and stores them in `pending_delivered` keyed by the message's `from` peer ID. The only filtering applied rejects non-TCP transport types; private/loopback/internal IPv4 and IPv6 addresses are accepted without restriction.

When a subsequent `ConnectionSync(from=attacker_id, to=victim_id)` arrives, `ConnectionSyncProcess::execute()` retrieves the stored addresses from `pending_delivered` and calls `try_nat_traversal` for each one. `try_nat_traversal` opens a real TCP socket and retries the connection every ~200ms for up to 30 seconds.

There are two independent missing checks:
1. `respond_delivered` does not validate that `content.from` matches the actual peer ID of the session that sent the message.
2. Neither `respond_delivered` nor `ConnectionSyncProcess` validates that `listen_addrs` are globally routable (non-private, non-loopback) addresses.

### Impact Explanation
- **SSRF**: The victim node makes TCP connections to attacker-specified internal addresses (e.g., `10.0.0.1:8545`, `192.168.1.1:22`, `127.0.0.1:6379`). This can interact with internal services that trust connections from localhost or the local network.
- **Port scanning**: The attacker can probe internal network topology by observing timing differences (connection success vs. timeout).
- **Resource exhaustion**: Each `ConnectionRequest`/`ConnectionSync` pair spawns a 30-second retry loop. Up to 24 addresses per request (`ADDRS_COUNT_LIMIT`) can be targeted simultaneously. Multiple attackers or rapid reconnection (using different peer IDs to bypass the 2-minute `HOLE_PUNCHING_INTERVAL` per-key rate limit) can exhaust file descriptors and thread pool capacity.

### Likelihood Explanation
The attacker only needs a single P2P connection to the victim node — a standard, publicly reachable CKB node. No special privileges, leaked keys, or majority hashpower are required. The two-message sequence (`ConnectionRequest` then `ConnectionSync`) is trivially constructable using the published molecule schema.

### Recommendation
1. **Validate `from` against the actual session peer ID**: In `ConnectionRequestProcess::execute`, reject messages where `content.from` does not match the peer ID of `self.peer` (the actual connected session).
2. **Filter private/loopback addresses**: In `respond_delivered`, reject any `listen_addr` whose IP component is a loopback address (`127.0.0.0/8`, `::1`), link-local (`169.254.0.0/16`, `fe80::/10`), or RFC-1918 private range (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`).
3. **Authenticate the `ConnectionSync` sender**: In `ConnectionSyncProcess::execute`, verify that the peer sending the sync message is the peer identified by `content.from` before retrieving and acting on `pending_delivered` entries.

### Proof of Concept
```
1. Attacker (peer_id=A) establishes a P2P connection to victim (peer_id=V).

2. Attacker sends:
   ConnectionRequest {
     from: A,
     to: V,
     max_hops: 0,
     route: [],
     listen_addrs: [/ip4/192.168.1.100/tcp/6379]  // internal Redis
   }

3. Victim's respond_delivered():
   - Passes the TCP/IPv4 filter (line 204-210)
   - Stores pending_delivered[A] = ([/ip4/192.168.1.100/tcp/6379], now)
   - Sends ConnectionRequestDelivered back to attacker

4. Attacker sends:
   ConnectionSync {
     from: A,
     to: V,
     route: []
   }

5. Victim's ConnectionSyncProcess::execute():
   - self_peer_id == content.to (V == V) → enters the passive branch
   - listens_info = pending_delivered.get(&A) → [/ip4/192.168.1.100/tcp/6379]
   - Spawns try_nat_traversal(bind_addr, /ip4/192.168.1.100/tcp/6379)

6. try_nat_traversal retries TCP connect to 192.168.1.100:6379 every ~200ms
   for up to 30 seconds. If Redis is listening, the TCP handshake completes
   and a raw_session is opened to it.

Repeat with different peer IDs (or wait 2 minutes) to scan additional ports.
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L98-124)
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
