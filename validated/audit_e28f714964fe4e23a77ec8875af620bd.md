The full attack path is now clear from the code. Let me trace it precisely.

### Title
Unauthenticated SSRF via Hole-Punching Protocol: Attacker-Controlled `listen_addrs` Stored in `pending_delivered` Trigger Outbound TCP Connections to Arbitrary Addresses — (`network/src/protocols/hole_punching/component/connection_request.rs`, `connection_sync.rs`)

---

### Summary

An unprivileged peer directly connected to a CKB node can cause that node to make repeated outbound TCP connections to any attacker-specified IPv4/IPv6 address (including loopback and internal network addresses) by sending two crafted hole-punching protocol messages. No authentication, PoW, or privileged role is required.

---

### Finding Description

The hole-punching protocol processes three message types: `ConnectionRequest`, `ConnectionRequestDelivered`, and `ConnectionSync`. The vulnerability is a two-step exploit:

**Step 1 — Poison `pending_delivered` via `ConnectionRequest`:**

When the target node receives a `ConnectionRequest` where `content.to == self_peer_id`, it calls `respond_delivered`. [1](#0-0) 

Inside `respond_delivered`, the attacker-supplied `listen_addrs` are filtered — but only to remove non-TCP transports and addresses lacking an IP component. Loopback (`127.0.0.1`), RFC-1918 private ranges, and arbitrary ports are **not filtered out**: [2](#0-1) 

The surviving addresses are then stored in `pending_delivered` keyed by the attacker-supplied `from` peer ID — with **no validation that `from` matches the actual sender's peer ID**: [3](#0-2) 

**Step 2 — Trigger `try_nat_traversal` via `ConnectionSync`:**

The attacker then sends a `ConnectionSync` with `from=FAKE_ID`, `to=TARGET_NODE_ID`, `route=[]`. When the node determines it is the `to` target, it looks up `pending_delivered[content.from]` — retrieving the attacker-planted addresses: [4](#0-3) 

It then spawns an async task that calls `try_nat_traversal` for each stored address: [5](#0-4) 

`try_nat_traversal` makes real TCP `connect()` calls in a retry loop for up to **30 seconds** at ~200ms intervals (~150 attempts per address): [6](#0-5) 

If a connection succeeds, it is promoted to a full P2P raw session: [7](#0-6) 

---

### Impact Explanation

- **Internal network port scanning**: The attacker can enumerate open ports on `127.0.0.1`, `10.x.x.x`, `192.168.x.x`, etc. by observing timing differences (connection refused vs. timeout).
- **Connection to malicious endpoints**: If the attacker controls a server at the specified address, the node will establish a raw P2P session with it, bypassing normal peer discovery and admission controls.
- **Resource exhaustion**: Each triggered `try_nat_traversal` runs for 30 seconds in a spawned task; multiple `ConnectionRequest` messages with different fake `from` IDs (bypassing the per-`from` `HOLE_PUNCHING_INTERVAL` rate limit) can spawn many concurrent tasks.

---

### Likelihood Explanation

The attack requires only a standard P2P connection to the target node — no special privileges, no leaked keys, no majority hashpower. Both crafted messages are syntactically valid and pass all existing structural checks. The two-message sequence is trivially scriptable.

---

### Recommendation

1. **Validate sender identity**: In `ConnectionRequest` processing, verify that `content.from` matches the actual sender's authenticated peer ID (available from the session context). Reject messages where `from` does not match the sender.
2. **Filter non-routable addresses**: In `respond_delivered`, reject `listen_addrs` that resolve to loopback, link-local, or RFC-1918 private addresses before storing them in `pending_delivered`.
3. **Correlate `ConnectionSync` sender**: In `ConnectionSyncProcess::execute`, verify that the sender of the `ConnectionSync` is the peer whose `from` ID is in `pending_delivered`, not just any connected peer.

---

### Proof of Concept

```
1. Connect to target CKB node as a normal peer (peer session established).

2. Send ConnectionRequest:
     from       = <random fake PeerId, e.g. FAKE_ID>
     to         = <target node's own PeerId>
     listen_addrs = [/ip4/127.0.0.1/tcp/8114/p2p/<FAKE_ID bytes>]
     route      = []
     max_hops   = 3

   → Target node calls respond_delivered, stores
     pending_delivered[FAKE_ID] = ([/ip4/127.0.0.1/tcp/8114/...], now)

3. Send ConnectionSync:
     from  = FAKE_ID
     to    = <target node's own PeerId>
     route = []

   → Target node finds pending_delivered[FAKE_ID],
     spawns try_nat_traversal(bind_addr, /ip4/127.0.0.1/tcp/8114/...)
     which calls socket.connect("127.0.0.1:8114") repeatedly for 30s.

4. Observe: TCP SYN packets arrive at 127.0.0.1:8114 (the RPC port).
   Repeat with different FAKE_IDs and different target ports/IPs to scan
   the internal network.
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

**File:** network/src/protocols/hole_punching/component/mod.rs (L65-84)
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
```
