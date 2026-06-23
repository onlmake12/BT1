### Title
Unauthenticated Remote Peer Can Force Victim Node to Initiate TCP Connections to Arbitrary Addresses via Hole-Punching Protocol — (`network/src/protocols/hole_punching/`)

### Summary

An unprivileged remote peer connected over P2P can craft a two-message sequence (`ConnectionRequest` followed by `ConnectionSync`) that causes the victim node to spawn async tasks making repeated TCP connection attempts to any attacker-specified IP address — including loopback (`127.0.0.1`), RFC-1918 private ranges, and link-local addresses — for up to 30 seconds per trigger. The address filter applied before storing addresses in `pending_delivered` only rejects non-TCP transports and addresses lacking an IP component; it performs no validation of the IP address value itself.

---

### Finding Description

**Step 1 — Poisoning `pending_delivered` via `ConnectionRequest`**

When the victim node receives a `ConnectionRequest` whose `to` field equals its own peer ID, `ConnectionRequestProcess::execute` calls `respond_delivered`: [1](#0-0) 

Inside `respond_delivered`, the attacker-supplied `listen_addrs` are filtered before being stored: [2](#0-1) 

The filter rejects non-TCP transports and addresses without an `Ip4`/`Ip6` component, but **performs no check on the IP address value**. Addresses such as `/ip4/127.0.0.1/tcp/8080` and `/ip4/10.0.0.1/tcp/8080` pass unconditionally. The surviving addresses are then stored verbatim: [3](#0-2) 

The `from` field in the `ConnectionRequest` is fully attacker-controlled — the code never verifies that it matches the actual sender's peer ID. The attacker can therefore use arbitrary fake `from` peer IDs, bypassing the 2-minute `HOLE_PUNCHING_INTERVAL` cooldown (which is keyed by `from_peer_id`) by rotating fake identities.

**Step 2 — Triggering NAT traversal via `ConnectionSync`**

When the victim subsequently receives a `ConnectionSync` whose `to` equals its own peer ID, `ConnectionSyncProcess::execute` looks up `pending_delivered` by `content.from` and passes every stored address directly to `try_nat_traversal`: [4](#0-3) 

No IP address validation occurs at this point either.

**Step 3 — `try_nat_traversal` makes real TCP connections**

`try_nat_traversal` converts the `Multiaddr` to a `SocketAddr` and then enters a retry loop that attempts `socket.connect(net_addr)` every ~200 ms for up to 30 seconds: [5](#0-4) 

There is no IP address guard anywhere in this function. A `SocketAddr` of `127.0.0.1:PORT` is accepted and connected to without restriction.

**Rate-limiter analysis**

The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)` at 1 request/second: [6](#0-5) 

Because `from` is attacker-controlled and unverified, the attacker can rotate fake `from` peer IDs to issue many triggers per second, each spawning a new 30-second async task.

---

### Impact Explanation

- **SSRF / internal network scanning**: The victim node makes outbound TCP connections to attacker-specified addresses. An attacker can probe services bound to `127.0.0.1` (e.g., RPC port 8114, metrics, admin interfaces) or any RFC-1918 host reachable from the victim's network. A successful TCP handshake is observable via timing side-channels or by controlling the target service.
- **Resource exhaustion**: Each trigger spawns a `runtime::spawn` task that loops for 30 seconds. With `ADDRS_COUNT_LIMIT = 24` addresses per request and rotating fake peer IDs, an attacker can accumulate hundreds of concurrent tasks, each consuming socket descriptors and CPU.

---

### Likelihood Explanation

The attacker only needs a standard P2P connection to the victim — no special privileges, no PoW, no key material. The victim's peer ID is exchanged during normal connection setup. The two-message sequence is straightforward to construct from the published molecule schema. The missing guard is a single missing IP-range check.

---

### Recommendation

In `respond_delivered` (`connection_request.rs`) and in `ConnectionRequestDeliveredProcess::try_nat_traversal` (`connection_request_delivered.rs`), add an IP address validation step that rejects:
- Loopback (`127.0.0.0/8`, `::1`)
- Private/RFC-1918 (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`)
- Link-local (`169.254.0.0/16`, `fe80::/10`)
- Any other non-globally-routable range

This check should be applied before addresses are stored in `pending_delivered` and before they are passed to `try_nat_traversal`. Additionally, verify that the `from` field in a `ConnectionRequest` matches the actual sender's authenticated peer ID to prevent fake-identity rotation.

---

### Proof of Concept

```
1. Attacker connects to victim as a normal P2P peer.
   Victim peer ID = VICTIM_PID (obtained from Identify handshake).

2. Attacker crafts ConnectionRequest:
     from       = FAKE_PID_1   (any valid PeerId bytes)
     to         = VICTIM_PID
     max_hops   = 6
     route      = []
     listen_addrs = [/ip4/127.0.0.1/tcp/8114/p2p/FAKE_PID_1]
                    (up to 24 addresses, including RFC-1918 targets)

3. Victim receives it; self_peer_id == content.to → respond_delivered() called.
   Filter passes /ip4/127.0.0.1/tcp/8114 (TCP + Ip4 present, no IP-range check).
   pending_delivered[FAKE_PID_1] = ([/ip4/127.0.0.1/tcp/8114/...], now)

4. Attacker crafts ConnectionSync:
     from  = FAKE_PID_1
     to    = VICTIM_PID
     route = []

5. Victim receives it; self_peer_id == content.to → looks up pending_delivered[FAKE_PID_1].
   Spawns runtime::spawn(try_nat_traversal(bind_addr, /ip4/127.0.0.1/tcp/8114/...))

6. try_nat_traversal loops for 30 s, calling TcpSocket::connect(127.0.0.1:8114) every ~200 ms.
   → Victim's RPC port (or any internal service) receives repeated TCP SYN packets.

7. Repeat steps 2–6 with FAKE_PID_2, FAKE_PID_3, … to accumulate concurrent tasks
   and scan additional internal addresses.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
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
