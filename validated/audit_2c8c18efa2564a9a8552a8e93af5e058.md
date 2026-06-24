Audit Report

## Title
Unauthenticated `from` field in `ConnectionRequest` allows arbitrary NAT traversal redirection and resource exhaustion — (`network/src/protocols/hole_punching/component/connection_request.rs`, `connection_sync.rs`)

## Summary
The hole-punching protocol stores `listen_addrs` from a `ConnectionRequest` message into `pending_delivered` keyed by the wire-level `from` field, with no verification that `from` matches the authenticated peer ID of the actual sending session. A subsequent `ConnectionSync` with a matching `from` field causes the victim to spawn `try_nat_traversal` tasks that retry TCP `connect()` for up to 30 seconds against attacker-controlled addresses. Using N distinct synthetic `from` peer IDs bypasses the per-pair `forward_rate_limiter`, enabling N concurrent traversal tasks and, on success, promoting attacker-controlled endpoints to full P2P sessions.

## Finding Description
In `connection_request.rs`, `ConnectionRequestProcess` holds a `peer: PeerIndex` field representing the authenticated session, but `execute()` never checks that `content.from` matches the peer ID authenticated for that session. [1](#0-0) 

When `self_peer_id == &content.to`, `respond_delivered` is called with the fully wire-controlled `content.from` and `content.listen_addrs`: [2](#0-1) 

Inside `respond_delivered`, the filter passes any TCP multiaddr containing an IP4 or IP6 component: [3](#0-2) 

The filtered addresses are stored in `pending_delivered` keyed by the wire-level `from_peer_id`: [4](#0-3) 

`ConnectionSyncProcess` has no `peer` field at all — it cannot even reference the sender's authenticated session identity: [5](#0-4) 

When `self_peer_id == &content.to` and `content.route` is empty, the code looks up `content.from` (wire-controlled) in `pending_delivered`: [6](#0-5) 

Every stored address is passed to `try_nat_traversal`, which retries TCP `connect()` for up to 30 seconds in a spawned async task: [7](#0-6) [8](#0-7) 

On success, `raw_session` promotes the stream to a full P2P session: [9](#0-8) 

**Why existing guards fail:**

The session-level `rate_limiter` is keyed by `(session_id, msg.item_id())` and allows 30 req/s per session per message type — this is the only binding to the real session: [10](#0-9) 

The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)` — using N distinct synthetic `from` peer IDs creates N distinct keys, fully bypassing per-pair throttling: [11](#0-10) 

`pending_delivered` is only pruned every 5 minutes with a 5-minute `TIMEOUT`, giving the attacker a large accumulation window: [12](#0-11) [13](#0-12) 

## Impact Explanation
From a single connected session, the attacker can send 30 `ConnectionRequest` + 30 `ConnectionSync` messages per second, each with a distinct synthetic `from` peer ID. Each `ConnectionSync` spawns an async task running `try_nat_traversal` for up to 30 seconds. After 30 seconds of sustained attack, ~900 concurrent tasks are active, each making repeated TCP connection attempts to attacker-controlled addresses. If the attacker's servers speak the CKB P2P protocol, `raw_session` promotes them to peers, enabling an eclipse attack that can cause consensus deviation. This maps to **High** (network congestion with few costs; potential eclipse of a node) and potentially **Critical** (consensus deviation via eclipse).

## Likelihood Explanation
The attacker requires only one normal, unprivileged P2P connection to the victim. The victim's peer ID is public. The two-message sequence (`ConnectionRequest` + `ConnectionSync`) is trivially crafted with arbitrary wire-level fields. No PoW, key material, or privileged role is required. The `forward_rate_limiter` is fully bypassable with synthetic peer IDs. The attack is repeatable and automatable.

## Recommendation
1. In `ConnectionRequestProcess::respond_delivered`, verify that `from_peer_id` matches the authenticated peer ID of `self.peer` (the actual session). Reject the message if they differ.
2. Add a `peer: PeerIndex` field to `ConnectionSyncProcess` and, before looking up `pending_delivered`, verify that `content.from` matches the authenticated peer ID of that session.
3. Alternatively, key `pending_delivered` by `(SessionId, PeerId)` and require the `ConnectionSync` to arrive on the same session as the original `ConnectionRequest`.

## Proof of Concept
```
1. Attacker (session S, authenticated peer A) connects to victim V.
2. For i in 1..N:
     A sends ConnectionRequest {
       from = synthetic_peer_id_i,   // arbitrary, not A
       to   = V_peer_id,
       listen_addrs = [/ip4/1.2.3.4/tcp/9999],
       route = [], max_hops = 6
     }
     → V: pending_delivered[synthetic_peer_id_i] = ([/ip4/1.2.3.4/tcp/9999], now)
3. For i in 1..N:
     A sends ConnectionSync {
       from = synthetic_peer_id_i,
       to   = V_peer_id,
       route = []
     }
     → V: looks up pending_delivered[synthetic_peer_id_i]
     → V: runtime::spawn(try_nat_traversal(bind_addr, /ip4/1.2.3.4/tcp/9999))
     → V: retries TCP connect() to 1.2.3.4:9999 for up to 30 seconds
4. Attacker's server at 1.2.3.4:9999 accepts; raw_session() promotes it to a peer.
5. After 30 seconds at 30 req/s: ~900 concurrent try_nat_traversal tasks active.
   Peer slots fill with attacker-controlled nodes → eclipse attack.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L85-91)
```rust
pub(crate) struct ConnectionRequestProcess<'a> {
    message: packed::ConnectionRequestReader<'a>,
    protocol: &'a mut HolePunching,
    peer: PeerIndex,
    p2p_control: &'a ServiceAsyncControl,
    msg_item_id: u32,
}
```

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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L51-57)
```rust
pub(crate) struct ConnectionSyncProcess<'a> {
    message: packed::ConnectionSyncReader<'a>,
    protocol: &'a HolePunching,
    p2p_control: &'a ServiceAsyncControl,
    bind_addr: Option<SocketAddr>,
    msg_item_id: u32,
}
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

**File:** network/src/protocols/hole_punching/component/mod.rs (L65-68)
```rust
    let timeout_duration = Duration::from_secs(30);
    let start_time = Instant::now();
    let mut retry_count = 0u32;
    while start_time.elapsed() < timeout_duration {
```

**File:** network/src/protocols/hole_punching/mod.rs (L25-28)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L44-46)
```rust
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```

**File:** network/src/protocols/hole_punching/mod.rs (L95-107)
```rust
        if self
            .rate_limiter
            .check_key(&(session_id, msg.item_id()))
            .is_err()
        {
            debug!(
                "process {} from {}; result is {}",
                item_name,
                session_id,
                status::StatusCode::TooManyRequests.with_context(msg.item_name())
            );
            return;
        }
```

**File:** network/src/protocols/hole_punching/mod.rs (L172-175)
```rust
        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```
