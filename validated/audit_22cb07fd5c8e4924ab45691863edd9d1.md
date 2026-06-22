Now I have the complete picture. Let me trace the full attack path precisely.

**Full attack chain:**

1. Attacker (peer `from`) is directly connected to victim (peer `to` = `self_peer_id`)
2. Attacker sends `ConnectionRequest` with `listen_addrs = [/ip4/127.0.0.1/tcp/8114/p2p/<from_peer_id>]`
3. In `respond_delivered`, the filter at lines 196–215 passes any `TransportType::Tcp` address that has an `Ip4` or `Ip6` component — **no loopback/private check** — so `127.0.0.1` passes and is stored in `pending_delivered`
4. Attacker directly sends a `ConnectionSync` with `from=attacker`, `to=victim`, `route=[]`
5. In `ConnectionSyncProcess::execute()`, the victim is `to`, so it reads `pending_delivered.get(&content.from)` (the attacker's stored addrs) and calls `try_nat_traversal` on each — making real TCP `connect()` calls to `127.0.0.1:8114` for up to 30 seconds with retries

The missing guard is in `respond_delivered` (connection_request.rs lines 196–215) and in `ConnectionSyncProcess::execute()` (connection_sync.rs lines 119–124): neither checks `IpAddr::is_loopback()`, `is_private()`, or `is_link_local()` before storing or dialing.

---

### Title
Unauthenticated SSRF via Unfiltered `listen_addrs` in Hole Punching `ConnectionRequest` / `ConnectionSync` — (`network/src/protocols/hole_punching/`)

### Summary
An unprivileged remote peer directly connected to a CKB node can craft a `ConnectionRequest` whose `listen_addrs` contain loopback or RFC-1918 addresses. The victim node stores these without IP-range validation and later dials them when it receives a `ConnectionSync`, causing outbound TCP connection attempts to arbitrary internal addresses (SSRF / internal service probing).

### Finding Description

**Step 1 — Parsing (`TryFrom` in `connection_request.rs`):**

The only validation on each `listen_addr` is that its embedded `/p2p/` peer ID, if present, matches `from`. No IP-range check is performed. [1](#0-0) 

**Step 2 — Filtering and storage (`respond_delivered`):**

The filter accepts any `TransportType::Tcp` address that contains an `Ip4` or `Ip6` protocol component. `127.0.0.1`, `10.0.0.1`, `192.168.x.x`, `::1`, etc. all satisfy this condition. The accepted addresses are stored verbatim in `pending_delivered`. [2](#0-1) 

**Step 3 — Dialing (`ConnectionSyncProcess::execute`):**

When the victim later receives a `ConnectionSync` whose `from` matches the attacker's peer ID, it retrieves the stored addresses from `pending_delivered` and passes each one directly to `try_nat_traversal` — no re-filtering. [3](#0-2) 

**Step 4 — TCP connect loop (`try_nat_traversal`):**

`try_nat_traversal` calls `socket.connect(net_addr)` in a retry loop for up to 30 seconds, making real TCP connection attempts to the attacker-supplied address. [4](#0-3) 

### Impact Explanation
The victim node makes repeated outbound TCP connections to attacker-controlled IP:port pairs, including loopback (`127.0.0.1`) and RFC-1918 addresses. This enables:
- **Internal service probing**: distinguishing open ports from closed/filtered ones on the host or LAN (connection success vs. `ECONNREFUSED` vs. timeout)
- **Interaction with local services**: e.g., the CKB RPC port (default 8114), internal HTTP APIs, databases — any TCP service reachable from the node host

### Likelihood Explanation
The attacker only needs a standard P2P connection to the victim. No special privileges, no PoW, no key material. The two-message sequence (`ConnectionRequest` then `ConnectionSync`) is trivially crafted. The rate limiter (`forward_rate_limiter`, 1 req/s per `(from, to, item_id)` tuple) slows but does not prevent the attack.

### Recommendation
In `respond_delivered`, after the `TransportType::Tcp` branch, extract the IP from the `Multiaddr` and reject non-globally-routable addresses:

```rust
// pseudo-code
if let Some(ip) = get_ip(&addr) {
    if ip.is_loopback() || ip.is_private() || ip.is_link_local()
       || ip.is_unspecified() || ip.is_multicast() {
        return None; // filter out
    }
}
```

Apply the same guard in `ConnectionSyncProcess::execute()` before spawning `try_nat_traversal` tasks, as a defense-in-depth measure.

### Proof of Concept
```
1. Attacker connects to victim CKB node (standard P2P handshake).
2. Attacker sends HolePunchingMessage::ConnectionRequest {
       from: <attacker_peer_id>,
       to:   <victim_peer_id>,
       max_hops: 6,
       route: [],
       listen_addrs: [/ip4/127.0.0.1/tcp/8114/p2p/<attacker_peer_id>],
   }
   → victim stores /ip4/127.0.0.1/tcp/8114/p2p/<attacker_peer_id>
     in pending_delivered[attacker_peer_id]

3. Attacker sends HolePunchingMessage::ConnectionSync {
       from: <attacker_peer_id>,
       to:   <victim_peer_id>,
       route: [],
   }
   → victim retrieves pending_delivered[attacker_peer_id]
   → victim calls try_nat_traversal(bind_addr, /ip4/127.0.0.1/tcp/8114/...)
   → victim makes TCP connect() to 127.0.0.1:8114 (CKB RPC) repeatedly for 30s

4. Observe on the victim host: `ss -tn | grep 8114` shows outbound
   connections from the node process to 127.0.0.1:8114.
   Repeat with /ip4/10.0.0.1/tcp/22 to probe internal SSH, etc.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L41-61)
```rust
        let listen_addrs: Vec<Multiaddr> = value
            .listen_addrs()
            .iter()
            .map(
                |raw| match Multiaddr::try_from(raw.bytes().raw_data().to_vec()) {
                    Ok(mut addr) => {
                        if let Some(peer_id) = extract_peer_id(&addr) {
                            if peer_id != from {
                                return Err(StatusCode::InvalidListenAddrLen
                                    .with_context("peer id in listen address is invalid"));
                            }
                        } else {
                            addr.push(Protocol::P2P(Cow::Borrowed(from.as_bytes())));
                        }
                        Ok(addr)
                    }
                    Err(_) => Err(StatusCode::InvalidListenAddrLen
                        .with_context("the listen address is invalid")),
                },
            )
            .collect::<Result<Vec<_>, _>>()?;
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
