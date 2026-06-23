#Vulnerability found.

---

### Title
Unauthenticated `from` Field in `ConnectionRequest` Enables SSRF-Equivalent Outbound TCP Connections to Attacker-Controlled Addresses — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

The hole-punching protocol's `ConnectionRequest` and `ConnectionSync` handlers do not verify that the `from` peer ID in a message corresponds to the actual sending peer. An attacker connected to the local node can spoof `from = victim_peer_id` with attacker-controlled `listen_addrs`, poison `pending_delivered`, then immediately send a `ConnectionSync` to trigger `try_nat_traversal` — causing the local node to make repeated outbound TCP connections to arbitrary attacker-specified IP:port targets.

---

### Finding Description

**Step 1 — Poison `pending_delivered`:**

`ConnectionRequestProcess::execute` checks `self_peer_id == &content.to` and, if true, calls `respond_delivered`. There is no check that `content.from` matches the actual sender's peer ID (`self.peer`). [1](#0-0) 

Inside `respond_delivered`, after filtering `remote_listens` to TCP/IPv4/IPv6 addresses (which the attacker can trivially satisfy), the attacker-controlled addresses are stored unconditionally: [2](#0-1) 

The key is the attacker-supplied `from_peer_id` — any arbitrary peer ID the attacker chooses. [3](#0-2) 

**Step 2 — Trigger NAT traversal via `ConnectionSync`:**

`ConnectionSyncProcess::execute` also performs no verification that `content.from` matches the actual sender. When `route` is empty and `content.to == local_node`, it reads directly from `pending_delivered` using the attacker-supplied `content.from`: [4](#0-3) 

It then spawns `try_nat_traversal` tasks for every address in the retrieved list: [5](#0-4) 

**Step 3 — `try_nat_traversal` makes real TCP connections:**

`try_nat_traversal` opens a TCP socket and repeatedly attempts `socket.connect(net_addr)` for up to 30 seconds with retries, to whatever address was stored: [6](#0-5) 

**The complete two-message attack** (both sent by the attacker directly to the local node):
1. `ConnectionRequest { from: <any_peer_id>, to: <local_node_id>, listen_addrs: [<attacker_target_ip:port>] }`
2. `ConnectionSync { from: <same_peer_id>, to: <local_node_id>, route: [] }`

No relay, no third party, no special state required.

---

### Impact Explanation

The local node makes unsolicited outbound TCP connections to arbitrary IP:port targets specified by the attacker. This is an SSRF-equivalent primitive: the attacker can use the CKB node as a TCP probe to:
- Port-scan internal networks reachable from the node's host
- Reach internal services (databases, admin panels, metadata endpoints) not exposed to the public internet
- Potentially trigger side effects on services that act on TCP connection establishment

The `try_nat_traversal` loop retries for 30 seconds with ~200ms intervals — roughly 150 connection attempts per target address, and up to `ADDRS_COUNT_LIMIT = 24` targets per invocation. [7](#0-6) [8](#0-7) 

---

### Likelihood Explanation

Any peer that can establish a P2P connection to the local node can trigger this. No privileged role, no leaked key, no Sybil attack required. The `forward_rate_limiter` (keyed by `(from, to, msg_item_id)`) allows 1 request/second per tuple, but the attacker can rotate `from` peer IDs to bypass it entirely. [9](#0-8) 

---

### Recommendation

1. **Authenticate `from`**: In `ConnectionRequestProcess::respond_delivered`, verify that `from_peer_id` matches the actual peer ID of the session sender (`self.peer`). Reject the message if they differ.
2. **Authenticate `from` in `ConnectionSync`**: Similarly, verify that `content.from` matches the actual sender's peer ID before looking up `pending_delivered`.
3. **Bind `pending_delivered` to session**: Store the session ID alongside the entry and require the `ConnectionSync` to arrive from the same session.

---

### Proof of Concept

```
1. Attacker connects to local node (peer A, session S).
2. Attacker sends:
     ConnectionRequest {
       from: <victim_peer_id>,   // spoofed — not A's real peer ID
       to:   <local_node_id>,
       listen_addrs: [/ip4/192.168.1.100/tcp/6379],  // attacker target
       route: [],
       max_hops: 6
     }
   → respond_delivered fires (to == local_node_id)
   → pending_delivered[victim_peer_id] = ([/ip4/192.168.1.100/tcp/6379], now)

3. Attacker immediately sends:
     ConnectionSync {
       from: <victim_peer_id>,   // same spoofed ID
       to:   <local_node_id>,
       route: []
     }
   → execute() branch: route empty, to == local_node_id
   → listens_info = pending_delivered[victim_peer_id]
   → try_nat_traversal spawned → TCP connect loop to 192.168.1.100:6379

Result: local node makes ~150 TCP connection attempts to 192.168.1.100:6379
        (an internal Redis instance, or any other target).
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L155-160)
```rust
    async fn respond_delivered(
        &mut self,
        from_peer_id: PeerId,
        to_peer_id: &PeerId,
        remote_listens: Vec<Multiaddr>,
    ) -> Status {
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

**File:** network/src/protocols/hole_punching/component/mod.rs (L64-66)
```rust
    // total time
    let timeout_duration = Duration::from_secs(30);
    let start_time = Instant::now();
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L68-85)
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
        )
```

**File:** network/src/protocols/hole_punching/mod.rs (L27-28)
```rust
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L255-257)
```rust
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```
