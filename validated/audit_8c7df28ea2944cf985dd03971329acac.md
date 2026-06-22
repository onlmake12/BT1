Now I have a complete picture of the code. Let me trace the full attack path precisely.

**Step 1 — `ConnectionRequest` → `respond_delivered` → `pending_delivered.insert`**

In `connection_request.rs` `execute()`, when `self_peer_id == &content.to`, `respond_delivered` is called with `content.from` (fully attacker-controlled) and `content.listen_addrs` (also fully attacker-controlled). [1](#0-0) 

Inside `respond_delivered`, the `remote_listens` filter only strips Memory/Onion/Ws/Wss/Tls transports — valid TCP/IP4 and IP6 multiaddrs pass through unchanged. [2](#0-1) 

The filtered attacker-controlled addresses are then stored in `pending_delivered` keyed by `from_peer_id` — which is `content.from`, a field the attacker writes freely. [3](#0-2) 

**Step 2 — `ConnectionSync` → `pending_delivered.get(&content.from)` → `try_nat_traversal`**

In `connection_sync.rs` `execute()`, when `self_peer_id == &content.to`, the code looks up `content.from` (again fully attacker-controlled) in `pending_delivered`: [4](#0-3) 

Every address returned is passed to `try_nat_traversal`, which retries TCP `connect()` for up to **30 seconds** to the attacker-controlled endpoint: [5](#0-4) [6](#0-5) 

On success, `raw_session` is called, promoting the TCP stream to a full P2P session with the attacker's endpoint: [7](#0-6) 

**Missing invariant:** There is no check anywhere that:
- The `from` field in `ConnectionRequest` matches the actual sender's peer ID
- The `from` field in `ConnectionSync` matches the actual sender's peer ID
- The sender of `ConnectionSync` is the same session as the sender of the `ConnectionRequest`

**Rate-limiter bypass:** The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)`. The attacker can use N distinct synthetic `from` peer IDs across N `ConnectionRequest` messages, storing N entries in `pending_delivered`, then fire N `ConnectionSync` messages to trigger N concurrent `try_nat_traversal` tasks — all within the 30 req/s `rate_limiter` cap. [8](#0-7) 

The `pending_delivered` map is only pruned every 5 minutes (`CHECK_INTERVAL`) with a 5-minute `TIMEOUT`, giving the attacker a large window. [9](#0-8) [10](#0-9) 

---

### Title
Unauthenticated `listen_addrs` in `ConnectionRequest` allows attacker to redirect victim's NAT traversal to arbitrary endpoints — (`network/src/protocols/hole_punching/component/connection_request.rs`, `connection_sync.rs`)

### Summary
An unprivileged peer directly connected to a victim CKB node can inject attacker-controlled IP addresses into the victim's `pending_delivered` map via a crafted `ConnectionRequest`, then trigger outbound TCP connections to those addresses via a `ConnectionSync` message. No cryptographic or session-identity check ties the `from` field or `listen_addrs` to the actual sender.

### Finding Description
The hole-punching protocol stores the `listen_addrs` from a `ConnectionRequest` message into `pending_delivered` keyed by the message's `from` field — both of which are fully attacker-controlled wire values with no binding to the actual TCP session. When a subsequent `ConnectionSync` arrives with a matching `from` field, the victim calls `try_nat_traversal` against every stored address, retrying TCP `connect()` for up to 30 seconds per address. On success, `raw_session` promotes the stream to a full P2P session. The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)`, so using N distinct synthetic `from` peer IDs lets the attacker bypass per-pair throttling and trigger N simultaneous traversal tasks within the 30 req/s global cap.

### Impact Explanation
The victim initiates outbound TCP connections to arbitrary attacker-controlled endpoints and, if the attacker's server speaks the CKB P2P protocol, those endpoints become peers. Repeating this with enough synthetic `from` IDs can fill the victim's outbound peer slots with attacker-controlled nodes, isolating it from honest peers (eclipse attack), enabling consensus deviation, double-spend relay manipulation, or transaction censorship.

### Likelihood Explanation
The attacker only needs one existing P2P connection to the victim — a normal, unprivileged peer. The two-message sequence is trivially crafted. No PoW, no key material, no privileged role is required. The rate limiter is bypassable with synthetic peer IDs.

### Recommendation
1. **Bind `listen_addrs` to the session identity**: in `respond_delivered`, verify that `content.from` matches the peer ID authenticated for `self.peer` (the actual session). Reject the message if they differ.
2. **Bind `ConnectionSync` to the session**: in `ConnectionSyncProcess::execute`, verify that `content.from` matches the authenticated peer ID of the actual sender session before looking up `pending_delivered`.
3. **Alternatively**, key `pending_delivered` by `(session_id, from_peer_id)` and require the `ConnectionSync` to arrive on the same session.

### Proof of Concept
```
1. Attacker (peer A, session S) connects to victim V.
2. A sends ConnectionRequest { from=A_peer_id, to=V_peer_id,
       listen_addrs=[/ip4/1.2.3.4/tcp/9999] }
   → V stores: pending_delivered[A_peer_id] = ([/ip4/1.2.3.4/tcp/9999], now)
3. A sends ConnectionSync { from=A_peer_id, to=V_peer_id, route=[] }
   → V looks up pending_delivered[A_peer_id], finds [/ip4/1.2.3.4/tcp/9999]
   → V spawns try_nat_traversal(bind_addr, /ip4/1.2.3.4/tcp/9999)
   → V retries TCP connect() to 1.2.3.4:9999 for up to 30 seconds
4. Attacker's server at 1.2.3.4:9999 accepts; raw_session() promotes it to a peer.
5. Repeat steps 2–4 with distinct synthetic from peer IDs to fill V's peer slots.
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

**File:** network/src/protocols/hole_punching/component/mod.rs (L49-65)
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
```

**File:** network/src/protocols/hole_punching/mod.rs (L24-28)
```rust
pub(crate) const HOLE_PUNCHING_INTERVAL: u64 = 2 * 60 * 1000; // 2 minutes
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L31-46)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;

/// Hole Punching Protocol
pub(crate) struct HolePunching {
    network_state: Arc<NetworkState>,
    bind_addr: Option<SocketAddr>,
    // Request timestamp recorded
    inflight_requests: HashMap<PeerId, u64>,
    // Delivered timestamp recorded
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```

**File:** network/src/protocols/hole_punching/mod.rs (L172-175)
```rust
        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```
