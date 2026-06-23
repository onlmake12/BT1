### Title
Attacker-Controlled `listen_addrs` in `ConnectionRequestDelivered` Causes Victim to Make Arbitrary Outbound TCP Connections — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

---

### Summary

An unprivileged connected peer can craft a `ConnectionRequestDelivered` P2P message with attacker-chosen IP:port combinations in `listen_addrs`. When the victim node is the originator of an in-flight hole-punching request (a normal operating state), it will call `try_nat_traversal` against every supplied address with no IP-range filtering, causing it to make repeated outbound TCP connections to arbitrary hosts — enabling SSRF, internal network port scanning, or connection-based amplification against third-party targets.

---

### Finding Description

**Step 1 — `listen_addrs` validation is peer-ID-only.**

`DeliverdContent::try_from` iterates over the supplied addresses and performs exactly one semantic check: if a peer ID is embedded in the multiaddr, it must equal `content.to`; otherwise the peer ID of `content.to` is appended automatically. [1](#0-0) 

There is no check on the IP address itself — RFC-1918 ranges (`10.x`, `172.16.x`, `192.168.x`), loopback (`127.x`), link-local, or any third-party public IP all pass without error.

**Step 2 — The only runtime gate is `inflight_requests`.**

Inside `execute()`, `try_nat_traversal` is reached only when three conditions hold simultaneously:

1. `content.route` is empty (no forwarding hops remain).
2. `self_peer_id == &content.from` (the local node is the declared originator).
3. `self.protocol.inflight_requests.remove(&content.to)` returns `Some`. [2](#0-1) 

Conditions 1 and 2 are fully attacker-controlled: the attacker sets `route = []` and `from = <victim peer ID>` (public knowledge). Condition 3 requires `content.to` to match a peer ID the victim is actively trying to reach.

**Step 3 — The attacker can satisfy condition 3.**

`inflight_requests` is populated in `notify()` whenever the victim needs more outbound connections (a normal operating state). The victim broadcasts the corresponding `ConnectionRequest` — including the `to` peer ID — to `sqrt(total_peers)` connected peers via gossip. [3](#0-2) 

Since the attacker is a connected peer, they have a non-trivial probability of receiving this broadcast and learning the exact `to` peer ID. The `inflight_requests` entry lives for up to 5 minutes (`TIMEOUT`), giving a wide exploitation window. [4](#0-3) 

**Step 4 — `try_nat_traversal` makes unrestricted TCP connections.**

Once called, `try_nat_traversal` converts each multiaddr to a `SocketAddr` and attempts TCP `connect()` in a retry loop for up to 30 seconds (~150 attempts per address, up to `ADDRS_COUNT_LIMIT = 24` addresses = ~3 600 total SYN packets per exploit invocation). [5](#0-4) 

There is no IP allowlist, denylist, or range check anywhere in `try_nat_traversal` or `create_socket`. [6](#0-5) 

---

### Impact Explanation

- **SSRF / internal network probing**: The victim's node process connects to RFC-1918 addresses chosen by the attacker, probing services on the victim's internal network (databases, admin panels, cloud metadata endpoints, etc.).
- **Third-party connection flood**: The victim sends repeated SYN packets to arbitrary public IPs, making the victim appear as the source of a port scan or connection flood against third parties.
- **Port-state oracle**: TCP connection success/failure (and timing) leaks port-open/closed state of internal hosts back through observable side-channels (e.g., connection latency, metrics counter `ckb_hole_punching_active_success_count`).

---

### Likelihood Explanation

The preconditions are all achievable by any peer that can open a single P2P connection to the victim:

| Precondition | Difficulty |
|---|---|
| One connected session | Trivial |
| Victim's peer ID | Public (advertised on the P2P network) |
| A `to` peer ID in `inflight_requests` | Low — observe the victim's own `ConnectionRequest` gossip broadcast, or brute-force within the 5-minute window |
| Timing (send before entry expires) | Easy — 5-minute window |

The victim only needs to be in a state where it wants more outbound connections, which is the default for any under-connected node.

---

### Recommendation

1. **IP-range filtering in `DeliverdContent::try_from`**: Reject any `listen_addr` whose IP component is loopback, link-local, RFC-1918, or otherwise non-routable before the address is stored or used.
2. **IP-range filtering in `try_nat_traversal`**: Add a guard at the top of the function that returns an error for any `net_addr` that is not a globally-routable unicast address.
3. **Authenticate the originator**: Before acting on `content.from == self_peer_id`, verify that the message arrived via the expected relay path rather than directly from an arbitrary connected peer.

---

### Proof of Concept

```
1. Attacker connects to victim (standard P2P handshake).

2. Attacker listens for ConnectionRequest gossip from victim.
   Victim broadcasts: ConnectionRequest { from=VICTIM_ID, to=TARGET_ID, ... }

3. Attacker sends to victim:
   ConnectionRequestDelivered {
     from        = VICTIM_ID,          // victim's own public peer ID
     to          = TARGET_ID,          // peer ID observed in step 2
     route       = [],                 // empty → no forwarding
     sync_route  = [],
     listen_addrs = [
       /ip4/192.168.1.1/tcp/8114/p2p/<TARGET_ID_bytes>,
       /ip4/10.0.0.1/tcp/22/p2p/<TARGET_ID_bytes>,
       /ip4/<third-party-public-ip>/tcp/80/p2p/<TARGET_ID_bytes>,
       ... (up to 24 entries)
     ]
   }

4. Victim's execute():
   - route.last() == None  → enters the "self is originator" branch
   - self_peer_id == content.from  ✓
   - inflight_requests.remove(TARGET_ID) == Some(start)  ✓
   - calls try_nat_traversal(ttl, attacker_listen_addrs)

5. try_nat_traversal spawns a task that retries TCP connect() to each
   attacker-supplied SocketAddr for up to 30 s.
   Victim's kernel sends SYN packets to 192.168.1.1:8114, 10.0.0.1:22,
   <third-party>:80, etc.

Assert: tcpdump on attacker-controlled host shows SYN from victim IP.
Assert: no IP validation error is ever returned.
```

### Citations

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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L147-176)
```rust
        match content.route.last() {
            Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
            None => {
                let self_peer_id = self.protocol.network_state.local_peer_id();
                if self_peer_id != &content.from {
                    // forward the message to the `from` peer
                    self.forward_delivered(&content.from).await
                } else {
                    // the current peer is the target peer, respond the sync back
                    if let Some(metrics) = ckb_metrics::handle() {
                        metrics.ckb_hole_punching_active_count.inc();
                    }

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

**File:** network/src/protocols/hole_punching/mod.rs (L27-28)
```rust
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L208-242)
```rust
            let mut inflight = Vec::new();
            for i in addrs {
                if let Some(to_peer_id) = extract_peer_id(&i.addr) {
                    let conn_req = {
                        let content = component::init_request(
                            from_peer_id,
                            &to_peer_id,
                            listen_addrs.clone(),
                        );
                        packed::HolePunchingMessage::new_builder()
                            .set(content)
                            .build()
                    };
                    let proto_id = SupportProtocols::HolePunching.protocol_id();

                    // Broadcast to a number of nodes equal to the square root of the total connection count using gossip.
                    let mut total = status.total.isqrt();
                    let _ignore = context
                        .filter_broadcast(
                            TargetSession::Filter(Box::new(move |_| {
                                total = total.saturating_sub(1);
                                total != 0
                            })),
                            proto_id,
                            conn_req.as_bytes(),
                        )
                        .await;
                    inflight.push(to_peer_id);
                }
            }

            let now = unix_time_as_millis();
            for peer_id in inflight {
                self.inflight_requests.insert(peer_id, now);
            }
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
