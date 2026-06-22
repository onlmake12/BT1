### Title
Spoofed `from` Field in `ConnectionRequestDelivered` Bypasses Inflight Guard, Triggering Outbound TCP Connections to Attacker-Controlled IPs — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

---

### Summary

An unprivileged connected peer can craft a `ConnectionRequestDelivered` message with a spoofed `from` field set to the victim's own peer ID, and a `listen_addrs` list of up to 24 TCP multiaddrs pointing to attacker-controlled IPs. Because `execute()` never verifies that `content.from` matches the actual sender's session peer ID, and because the attacker can learn a valid `to` peer ID from an observed `ConnectionRequest` broadcast, all guards pass and `try_nat_traversal` is called — causing the victim to initiate up to 24 outbound TCP connections to attacker-controlled addresses.

---

### Finding Description

**Missing sender-identity check in `execute()`**

`ConnectionRequestDeliveredProcess::execute()` parses `content.from` directly from the message payload and then compares it to the local peer ID:

```rust
let self_peer_id = self.protocol.network_state.local_peer_id();
if self_peer_id != &content.from {
    self.forward_delivered(&content.from).await
} else {
    // ... try_nat_traversal path
}
```

There is no check that `content.from` equals the peer ID of the session that actually sent the message. An attacker simply sets `from` = victim's own peer ID to enter the `else` branch. [1](#0-0) 

**`inflight_requests` guard is bypassable via broadcast observation**

The only remaining guard is:

```rust
let request_start = self.protocol.inflight_requests.remove(&content.to);
match request_start {
    Some(start) => { ... self.try_nat_traversal(ttl, content.listen_addrs); }
    None => StatusCode::Ignore ...
}
``` [2](#0-1) 

`inflight_requests` is populated when the victim node itself broadcasts a `ConnectionRequest` in `notify()`:

```rust
self.inflight_requests.insert(peer_id, now);
``` [3](#0-2) 

The broadcast is sent to `sqrt(total)` connected peers via `filter_broadcast`, which includes the attacker if they are connected. The broadcast carries `from=victim_peer_id` and `to=target_peer_id` in plaintext. The attacker reads both values and uses them to craft the spoofed reply. [4](#0-3) 

The `inflight_requests` entry persists for up to 5 minutes (`TIMEOUT = 5 * 60 * 1000 ms`), giving the attacker a wide window. [5](#0-4) 

**`listen_addrs` parsing silently appends `to`'s peer ID to bare TCP addresses**

In `DeliverdContent::try_from`, if an address has no embedded peer ID, `content.to`'s peer ID is appended without any validation that the address actually belongs to `content.to`:

```rust
} else {
    addr.push(Protocol::P2P(Cow::Borrowed(to.as_bytes())));
}
Ok(addr)
``` [6](#0-5) 

This means 24 bare TCP multiaddrs (e.g. `/ip4/1.2.3.4/tcp/9999`) are accepted and decorated with the target's peer ID, then passed directly to `try_nat_traversal`.

**`try_nat_traversal` opens TCP connections to all supplied addresses**

`try_nat_traversal` spawns an async task that calls `select_ok(tasks)`, running all connection futures concurrently. Each future retries TCP `connect()` in a loop for up to 30 seconds: [7](#0-6) [8](#0-7) 

---

### Impact Explanation

- **Port-scanning amplification**: The victim's IP initiates TCP SYNs to up to 24 attacker-chosen (IP, port) pairs. The victim's address appears as the scanner.
- **Connection-slot / resource exhaustion**: Each spawned task holds up to 24 `TcpSocket` objects retrying for 30 seconds. With the rate limiter at 1 req/sec per `(from, to, item_id)` tuple, and the victim potentially having multiple inflight requests to different peers, an attacker can sustain a continuous stream of spawned tasks.
- **Invariant violation**: The protocol's design intent is that `listen_addrs` in a `ConnectionRequestDelivered` are self-certified addresses of the `to` peer. The missing sender check completely breaks this invariant.

---

### Likelihood Explanation

- Requires only a standard P2P connection — no special privileges, no PoW, no key material.
- The `ConnectionRequest` broadcast is observable by any connected peer.
- The `inflight_requests` window is 5 minutes, giving ample time to craft and deliver the spoofed message.
- The exploit is deterministic and locally testable.

---

### Recommendation

1. **Verify sender identity**: At the start of `execute()`, assert that `content.from` equals the peer ID of the session that delivered the message (available via the session's peer registry). Reject with a ban if they differ.
2. **Validate address ownership**: Do not silently accept bare TCP addresses and append an arbitrary peer ID. Require that `listen_addrs` entries already carry the correct P2P component, or drop them.
3. **Scope `inflight_requests` removal**: Only remove an inflight entry when the message arrives from a peer that was actually in the forwarding route, not from any arbitrary connected peer.

---

### Proof of Concept

```
1. Attacker connects to victim node V (peer ID = V_id).
2. V broadcasts ConnectionRequest{from=V_id, to=T_id, listen_addrs=[...]}
   to sqrt(N) peers; attacker receives it and records V_id and T_id.
3. Attacker sends to V:
     ConnectionRequestDelivered {
       from        = V_id,          // spoofed to victim's own peer ID
       to          = T_id,          // learned from broadcast
       route       = [],            // empty → triggers the "self is from" branch
       sync_route  = [],
       listen_addrs = [
         /ip4/ATTACKER_IP_1/tcp/PORT_1,
         /ip4/ATTACKER_IP_2/tcp/PORT_2,
         ... (×24, no /p2p/ suffix)
       ]
     }
4. V's execute():
   - content.from == V_id == self_peer_id  → enters "respond_sync + try_nat_traversal" branch
   - inflight_requests.remove(T_id) = Some(start)  → guard passes
   - try_nat_traversal spawned with 24 attacker IPs
5. Assert: 24 TCP SYN packets leave V toward ATTACKER_IP_1..24.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L62-65)
```rust
                        } else {
                            addr.push(Protocol::P2P(Cow::Borrowed(to.as_bytes())));
                        }
                        Ok(addr)
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L150-154)
```rust
                let self_peer_id = self.protocol.network_state.local_peer_id();
                if self_peer_id != &content.from {
                    // forward the message to the `from` peer
                    self.forward_delivered(&content.from).await
                } else {
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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L237-284)
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

        if tasks.is_empty() {
            return;
        }

        debug!("start NAT traversal");

        let control = self.p2p_control.clone();

        runtime::spawn(async move {
            runtime::delay_for(std::time::Duration::from_millis(ttl / 2)).await;
            if let Ok(((stream, addr), _)) = select_ok(tasks).await {
                debug!("NAT traversal success, addr: {:?}", addr);
                if let Some(metrics) = ckb_metrics::handle() {
                    metrics.ckb_hole_punching_active_success_count.inc();
                }
                let _ignore = control
                    .raw_session(
                        stream,
                        addr,
                        RawSessionInfo::outbound(TargetProtocol::Single(
                            SupportProtocols::Identify.protocol_id(),
                        )),
                    )
                    .await;
            }
        });
```

**File:** network/src/protocols/hole_punching/mod.rs (L28-28)
```rust
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L223-235)
```rust
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
```

**File:** network/src/protocols/hole_punching/mod.rs (L239-242)
```rust
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
