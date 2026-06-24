Audit Report

## Title
Unauthenticated `ConnectionSync` Forwarding Enables Unbounded Async Task Spawning and Resource Exhaustion — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

## Summary
A relay node unconditionally forwards a `ConnectionSync` message to whichever peer ID appears in `route.last()` with no verification that it previously participated in a legitimate hole-punching flow for that `(from, to)` pair. An attacker with a standard P2P connection can pre-populate `pending_delivered` at a target node via crafted `ConnectionRequest` messages, then inject crafted `ConnectionSync` messages through any relay connected to the target, causing the target to spawn unbounded async NAT traversal tasks — each creating TCP sockets and retrying for 30 seconds — exhausting file descriptors and async runtime resources sufficient to crash the node.

## Finding Description

**Step 1 — Pre-populate `pending_delivered` at the target:**

`ConnectionRequestProcess::execute` checks `self_peer_id == &content.to` and calls `respond_delivered`: [1](#0-0) 

`respond_delivered` filters listen addresses to TCP/IP only, then unconditionally inserts `from_peer_id → (remote_listens, now)` into `pending_delivered` with no authentication of the `from` identity: [2](#0-1) 

The only deduplication guard is a 2-minute interval check keyed by `from_peer_id`, trivially bypassed by cycling different `from` values: [3](#0-2) 

**Step 2 — Inject a crafted `ConnectionSync` at a relay:**

`ConnectionSyncProcess::execute` checks only route length and a `forward_rate_limiter` keyed by the fully attacker-controlled triple `(content.from, content.to, msg_item_id)`, then unconditionally calls `forward_sync(route.last())`: [4](#0-3) 

There is no check that the relay ever forwarded a `ConnectionRequestDelivered` for this `(from, to)` pair, and no check that the session sending the `ConnectionSync` is actually `content.from`. Any relay connected to the target can be weaponized.

**Step 3 — Relay forwards to the target:**

`forward_sync` resolves `target_peer_id` via the peer registry and sends the message with the route element popped: [5](#0-4) 

**Step 4 — Target spawns NAT traversal tasks:**

The target receives `ConnectionSync{route=[]}`. Since `route.last()` is `None` and `self_peer_id == content.to`, it looks up `pending_delivered[content.from]` (populated in Step 1) and spawns an async task via `runtime::spawn`: [6](#0-5) 

Each spawned task runs `select_ok` over up to `ADDRS_COUNT_LIMIT = 24` concurrent TCP connection attempts against attacker-controlled addresses: [7](#0-6) 

Each task loops for 30 seconds, creating a new TCP socket per retry iteration at ~200ms intervals (~150 iterations): [8](#0-7) 

**Why existing guards fail:**

- The per-session `rate_limiter` (keyed by `(session_id, msg_item_id)`) allows **30 `ConnectionSync` messages per second** per relay session — 30 task spawns/sec per attacker-controlled relay connection: [9](#0-8) 
- The `forward_rate_limiter` (keyed by `(from, to, msg_item_id)`) allows 1 req/sec per pair but is bypassed by cycling different `from` peer IDs: [10](#0-9) 
- `pending_delivered` cleanup runs every 5 minutes (`TIMEOUT = 5 * 60 * 1000`), so entries persist long enough for repeated exploitation: [11](#0-10) 

## Impact Explanation

**High — Vulnerabilities which could easily crash a CKB node.**

At 30 task spawns/sec per attacker relay connection, each task lasting up to 30 seconds, the attacker sustains ~900 concurrent async tasks per connection. Each task creates up to 24 TCP sockets per retry cycle (~150 iterations over 30 seconds), yielding up to ~21,600 concurrent TCP sockets. This exhausts the target's file descriptor limit and async runtime thread pool, crashing the node. Multiple attacker connections or relays amplify the effect.

## Likelihood Explanation

The attack requires only a standard P2P connection — no privileged access, no PoW, no leaked keys. The `ConnectionRequest` gossip broadcast means Step 1 can be executed without a direct connection to the target. Step 2 requires only that the attacker be connected to any relay that is connected to the target, a common topology in a well-connected P2P network. The two-step setup is straightforward to implement and fully repeatable.

## Recommendation

1. **Validate route provenance at the relay**: Before forwarding a `ConnectionSync`, the relay should verify it previously forwarded a `ConnectionRequestDelivered` for the same `(from, to)` pair. Maintain a short-lived set of legitimate `(from, to)` pairs for which the node acted as a relay in the `ConnectionRequestDelivered` phase, and reject `ConnectionSync` messages not matching a recorded entry.
2. **Authenticate `from` identity**: Require that the session sending a `ConnectionSync` matches `content.from` (i.e., the message must originate from the actual `from` peer, not be injected by a third party).
3. **Decouple `pending_delivered` from unauthenticated `ConnectionRequest`**: Only store `pending_delivered` entries for `from` peers with an existing or expected relationship, or add a challenge-response step before storing attacker-supplied listen addresses.
4. **Bound async task spawning**: Add a per-node cap on concurrently running NAT traversal tasks to limit the blast radius of any bypass.

## Proof of Concept

```
1. Attacker A connects to target T directly (or via gossip path).
2. A sends ConnectionRequest{from=A_id, to=T_id, listen_addrs=[A_tcp_addr], max_hops=6, route=[]} to T.
   T: self_peer_id == to → respond_delivered → pending_delivered[A_id] = ([A_tcp_addr], now).
3. A connects to relay R (which is connected to T).
4. A sends ConnectionSync{from=A_id, to=T_id, route=[T_id]} to R.
   R: route.last() = T_id → forward_sync(T_id) → peer_registry lookup → send_message_to(T_session)
   with forwarded message ConnectionSync{from=A_id, to=T_id, route=[]}.
5. T receives ConnectionSync{route=[]}.
   route.last() = None, self == to, pending_delivered[A_id] = Some([A_tcp_addr]).
   listen_addresses.first() = Some(...) → runtime::spawn(select_ok([try_nat_traversal(A_tcp_addr)])).
   → 30-second TCP retry loop spawned against attacker-controlled address.
6. Repeat step 4 at 30 msg/sec (session rate limit) to spawn 30 tasks/sec.
   Cycle A_id values to bypass forward_rate_limiter and pre-populate more pending_delivered entries.
   At steady state: ~900 concurrent tasks, each creating TCP sockets → file descriptor exhaustion → node crash.
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L82-99)
```rust
        if content.route.len() > MAX_HOPS as usize {
            return StatusCode::InvalidRoute.with_context("the route length is too long");
        }
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

        match content.route.last() {
            Some(next_peer_id) => self.forward_sync(next_peer_id).await,
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-163)
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

                            if tasks.is_empty() {
                                return StatusCode::Ignore.with_context("no valid listen address");
                            }

                            debug!(
                                "current peer is the target peer {}, start NAT traversal",
                                content.to
                            );

                            match self
                                .protocol
                                .network_state
                                .config
                                .listen_addresses
                                .first()
                                .cloned()
                            {
                                Some(listen_addr) => {
                                    let control: ServiceAsyncControl = self.p2p_control.clone();
                                    runtime::spawn(async move {
                                        if let Ok(((stream, addr), _)) = select_ok(tasks).await {
                                            debug!("NAT traversal success, addr: {:?}", addr);
                                            if let Some(metrics) = ckb_metrics::handle() {
                                                metrics
                                                    .ckb_hole_punching_passive_success_count
                                                    .inc();
                                            }

                                            let _ignore = control
                                                .raw_session(
                                                    stream,
                                                    addr,
                                                    RawSessionInfo::inbound(listen_addr),
                                                )
                                                .await;
                                        }
                                    });
                                    Status::ok()
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L178-210)
```rust
    async fn forward_sync(&self, peer_id: &PeerId) -> Status {
        let target_sid = self
            .protocol
            .network_state
            .peer_registry
            .read()
            .get_key_by_peer_id(peer_id);

        match target_sid {
            Some(next_peer) => {
                let content = forward_sync(self.message);
                let new_message = packed::HolePunchingMessage::new_builder()
                    .set(content)
                    .build()
                    .as_bytes();
                let proto_id = SupportProtocols::HolePunching.protocol_id();
                debug!(
                    "forward the sync to next peer {} (id: {})",
                    next_peer, peer_id
                );
                if let Err(error) = self
                    .p2p_control
                    .send_message_to(next_peer, proto_id, new_message)
                    .await
                {
                    StatusCode::ForwardError.with_context(error)
                } else {
                    Status::ok()
                }
            }
            None => StatusCode::Ignore.with_context("the next peer in the route is disconnected"),
        }
    }
```

**File:** network/src/protocols/hole_punching/mod.rs (L27-27)
```rust
const ADDRS_COUNT_LIMIT: usize = 24;
```

**File:** network/src/protocols/hole_punching/mod.rs (L28-28)
```rust
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L249-252)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (CHECK_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/hole_punching/mod.rs (L255-257)
```rust
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L65-68)
```rust
    let timeout_duration = Duration::from_secs(30);
    let start_time = Instant::now();
    let mut retry_count = 0u32;
    while start_time.elapsed() < timeout_duration {
```
