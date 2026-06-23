### Title
Unauthenticated `from` Field in Hole-Punching Protocol Enables SSRF-Equivalent Arbitrary Outbound TCP Connections — (`network/src/protocols/hole_punching/component/connection_request.rs`, `connection_sync.rs`)

---

### Summary

An unprivileged remote peer can cause the local CKB node to make unsolicited outbound TCP connections to arbitrary attacker-specified IP:port targets by sending two spoofed hole-punching protocol messages: a `ConnectionRequest` with a forged `from` peer ID and attacker-controlled `listen_addrs`, followed by a `ConnectionSync` referencing the same forged `from`. No authentication of the `from` field is performed at any point in the pipeline.

---

### Finding Description

The hole-punching protocol processes two message types whose `from` field is entirely attacker-controlled and is never verified against the actual sending session.

**Step 1 — `ConnectionRequest` poisons `pending_delivered`**

In `ConnectionRequestProcess::execute`, when `self_peer_id == &content.to` (i.e., the local node is the intended recipient), `respond_delivered` is called: [1](#0-0) 

Inside `respond_delivered`, after filtering `remote_listens` to TCP/IP addresses only, the function unconditionally inserts the attacker-supplied addresses into `pending_delivered` keyed by the attacker-supplied `from_peer_id`: [2](#0-1) 

There is no check anywhere in this path that the actual sending session (`self.peer`) corresponds to `content.from`. The `from` field is parsed from the wire message and used directly as the map key.

**Step 2 — `ConnectionSync` triggers `try_nat_traversal` to attacker addresses**

In `ConnectionSyncProcess::execute`, when `self_peer_id == &content.to`, the code reads `pending_delivered` using the attacker-controlled `content.from`: [3](#0-2) 

The retrieved addresses are passed directly to `try_nat_traversal`: [4](#0-3) 

Again, there is no check that the sender of `ConnectionSync` is the actual peer identified by `content.from`.

**Step 3 — `try_nat_traversal` makes real TCP connections**

`try_nat_traversal` in `component/mod.rs` performs repeated TCP `connect()` calls to the target address for up to 30 seconds with ~200ms retry intervals: [5](#0-4) 

The only address validation is that the multiaddr is TCP with an IPv4 or IPv6 component — any routable IP:port is accepted.

**Rate-limiting does not prevent the attack**

The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)`. Since `from` is attacker-controlled, the attacker can use a fresh synthetic peer ID per request to bypass the 1-req/sec limit entirely. [6](#0-5) 

The `HOLE_PUNCHING_INTERVAL` (2 minutes) check in `respond_delivered` is also per `from_peer_id`, so it is equally bypassable with distinct fake IDs. [7](#0-6) 

---

### Impact Explanation

The local CKB node makes unsolicited outbound TCP connections to arbitrary attacker-specified IP:port targets. This is a direct SSRF-equivalent: the attacker can probe internal network services, trigger connection-based side effects on third-party hosts, or exhaust local socket/file-descriptor resources by flooding with distinct fake peer IDs (up to `ADDRS_COUNT_LIMIT = 24` addresses per request, each retried for 30 seconds).

---

### Likelihood Explanation

Any peer that can establish a standard P2P connection to the local node can trigger this. No special privileges, no PoW, no key material. The two required messages (`ConnectionRequest` then `ConnectionSync`) can be sent back-to-back on the same session. The attack is fully local-testable.

---

### Recommendation

Bind the `from` field to the actual sending session. In `ConnectionRequestProcess`, verify that the session peer ID (`self.peer` resolved via the peer registry) matches `content.from` before inserting into `pending_delivered`. Similarly, in `ConnectionSyncProcess`, verify the sender session matches `content.from` before reading `pending_delivered`. If the hole-punching design requires relayed messages (where `from` is not the direct sender), the relay chain must be authenticated end-to-end, e.g., by requiring the originating peer to sign the `listen_addrs`.

---

### Proof of Concept

```
1. Attacker peer A establishes a standard P2P connection to local node L.
2. A generates a synthetic PeerId `fake_id` (any valid 32-byte Ed25519 public key).
3. A sends ConnectionRequest {
       from: fake_id,
       to:   L.peer_id,
       listen_addrs: [/ip4/192.168.1.1/tcp/22],   // attacker-chosen target
       route: [],
       max_hops: 1
   }
4. L.respond_delivered() inserts (fake_id -> ([/ip4/192.168.1.1/tcp/22/p2p/fake_id], now))
   into pending_delivered.
5. A sends ConnectionSync {
       from: fake_id,
       to:   L.peer_id,
       route: []
   }
6. L.ConnectionSyncProcess reads pending_delivered[fake_id], spawns try_nat_traversal
   tasks that issue TCP connect() to 192.168.1.1:22 repeatedly for 30 seconds.
7. Repeat with a new fake_id to bypass HOLE_PUNCHING_INTERVAL and rate limiter.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-115)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L119-124)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L24-27)
```rust
pub(crate) const HOLE_PUNCHING_INTERVAL: u64 = 2 * 60 * 1000; // 2 minutes
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
const ADDRS_COUNT_LIMIT: usize = 24;
```

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```
