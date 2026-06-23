### Title
Unvalidated `listen_addrs` in Hole-Punching Protocol Enables SSRF-like Internal TCP Port Scanning — (`network/src/protocols/hole_punching/component/connection_request.rs`, `connection_sync.rs`)

---

### Summary

An unprivileged remote P2P peer can cause a victim CKB node to initiate outbound TCP connections to arbitrary attacker-specified addresses — including loopback (`127.0.0.1`) and private network ranges — by exploiting the two-message hole-punching handshake. No authentication, PoW, or privileged role is required beyond being a connected peer.

---

### Finding Description

**Step 1 — Poison `pending_delivered` via `ConnectionRequest`**

`ConnectionRequestProcess::execute` checks `self_peer_id == &content.to` and, when true, calls `respond_delivered` with the attacker-supplied `listen_addrs`. [1](#0-0) 

Inside `respond_delivered`, the only filter applied to the attacker's addresses is:

```
TransportType::Tcp  AND  has Ip4 or Ip6 component
``` [2](#0-1) 

There is **no check** for loopback (`127.x.x.x`), unspecified (`0.0.0.0`), link-local, or RFC-1918 private ranges. An address like `/ip4/127.0.0.1/tcp/8114` passes the filter and is stored verbatim: [3](#0-2) 

**Step 2 — Trigger `try_nat_traversal` via `ConnectionSync`**

When the attacker subsequently sends a `ConnectionSync` with `content.from = attacker_peer_id` and `content.to = victim_peer_id`, `ConnectionSyncProcess::execute` retrieves the poisoned addresses from `pending_delivered` and spawns `try_nat_traversal` tasks for each one — with **no additional address validation**: [4](#0-3) 

**Step 3 — `try_nat_traversal` makes real TCP connections**

`try_nat_traversal` converts the multiaddr to a `SocketAddr` and enters a **30-second retry loop**, issuing a TCP `connect()` to the attacker-controlled address on every iteration (~200 ms interval): [5](#0-4) 

If the TCP handshake succeeds (e.g., a local service is listening on that port), the code calls `control.raw_session(stream, addr, RawSessionInfo::inbound(...))`, attempting to register the connection as an inbound P2P session: [6](#0-5) 

**Rate-limiting does not prevent the attack**

The `HOLE_PUNCHING_INTERVAL` guard (2 minutes) is keyed on `from_peer_id`. An attacker generating fresh peer IDs bypasses it entirely. The `forward_rate_limiter` is keyed on `(from, to, msg_item_id)` — same bypass applies. [7](#0-6) 

---

### Impact Explanation

| Impact | Detail |
|---|---|
| Internal TCP port scanning | Victim probes any IP:port the attacker specifies, including `127.0.0.1`, `10.x.x.x`, `192.168.x.x` |
| Unintended P2P session injection | If a local service accepts the TCP handshake, the victim registers it as an inbound P2P peer |
| Resource exhaustion | Up to 24 addresses × 30-second retry loops per request; multiple requests with fresh peer IDs multiply the load |

The `AddrNotAvailable` early-return path in `try_nat_traversal` is handled gracefully by `select_ok`; no panic occurs. [8](#0-7) 

---

### Likelihood Explanation

- Requires only a standard P2P connection — no key, no PoW, no privileged role.
- The victim's peer ID is public (exchanged during the identify handshake).
- The two-message sequence (`ConnectionRequest` → `ConnectionSync`) is the normal protocol flow; no protocol violation is needed.
- Generating fresh peer IDs to bypass rate limits is trivial.

---

### Recommendation

In `respond_delivered`, reject addresses whose IP component is loopback, unspecified (`0.0.0.0`/`::`), link-local, or RFC-1918 private before inserting into `pending_delivered`. The same guard should be applied in `ConnectionRequestDeliveredProcess::try_nat_traversal` for the active side. Example predicate (Rust):

```rust
fn is_globally_routable(ip: IpAddr) -> bool {
    !ip.is_loopback() && !ip.is_unspecified()
    && !ip.is_multicast() && !is_private(ip)
}
```

Apply this filter at the point where `remote_listens` is built in `respond_delivered` (line 196–215 of `connection_request.rs`).

---

### Proof of Concept

```
1. Connect to victim as peer A (any valid peer ID).
2. Send ConnectionRequest {
       from: peer_A_id,
       to:   victim_peer_id,       // victim's public peer ID
       listen_addrs: [/ip4/127.0.0.1/tcp/1],
       max_hops: 0,
       route: []
   }
   → victim stores (peer_A_id → [127.0.0.1:1]) in pending_delivered.

3. Send ConnectionSync {
       from: peer_A_id,
       to:   victim_peer_id,
       route: []
   }
   → victim spawns try_nat_traversal(bind_addr, /ip4/127.0.0.1/tcp/1).
   → victim issues TCP SYN to 127.0.0.1:1 repeatedly for 30 seconds.

4. Replace port 1 with 8114 (default RPC) or any internal service port
   to confirm open/closed status via timing side-channel or connection
   acceptance.
```

### Citations

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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L154-160)
```rust
                                            let _ignore = control
                                                .raw_session(
                                                    stream,
                                                    addr,
                                                    RawSessionInfo::inbound(listen_addr),
                                                )
                                                .await;
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L68-84)
```rust
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
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L99-102)
```rust
            Ok(Err(err)) => {
                if err.kind() == std::io::ErrorKind::AddrNotAvailable {
                    return Err(err);
                }
```
