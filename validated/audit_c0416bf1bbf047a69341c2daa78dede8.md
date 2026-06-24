Audit Report

## Title
Unbounded Async Task Spawning via Unvalidated Attacker-Controlled `listen_addrs` and Unbound `pending_delivered` Key in Hole-Punching Protocol — (`network/src/protocols/hole_punching/component/connection_sync.rs`, `connection_request.rs`)

## Summary

An unprivileged peer with a standard P2P connection can cause the victim node to spawn unbounded 30-second async tasks by sending crafted `ConnectionRequest` and `ConnectionSync` message pairs. The `pending_delivered` map is keyed by the message-level `content.from` field — never verified against the actual session peer ID — allowing an attacker to rotate synthetic peer IDs to bypass all per-key cooldowns. At the per-session rate limit of 30 messages/second, an attacker sustains ~900 concurrent spawned tasks (each running 24 concurrent TCP connection attempts) at steady state, exhausting async task and file-descriptor resources and crashing the node.

## Finding Description

**Root cause 1 — No IP-range filtering on attacker-supplied `listen_addrs`:**

In `respond_delivered()`, attacker-supplied addresses are filtered only by transport type (TCP) and IPv4/IPv6 presence:

```rust
TransportType::Tcp => {
    if addr.iter().any(|p| matches!(p, Protocol::Ip4(_) | Protocol::Ip6(_))) {
        Some(addr)
    } else {
        None
    }
}
``` [1](#0-0) 

No private (`10.x`, `192.168.x`), loopback (`127.x`), or link-local (`169.254.x`) address check exists. These addresses are stored verbatim in `pending_delivered`.

**Root cause 2 — `pending_delivered` keyed by message-level `from`, not session peer ID:**

```rust
self.protocol.pending_delivered.insert(from_peer_id, (remote_listens, now));
``` [2](#0-1) 

`from_peer_id` is `content.from` — a field in the message body, never cross-checked against the actual session's peer identity. `ConnectionRequestProcess` has a `peer: PeerIndex` field but it is never used to validate `content.from`. [3](#0-2) 

**Root cause 3 — `ConnectionSyncProcess` has no session-to-`from` binding:**

```rust
pub(crate) struct ConnectionSyncProcess<'a> {
    message: packed::ConnectionSyncReader<'a>,
    protocol: &'a HolePunching,
    p2p_control: &'a ServiceAsyncControl,
    bind_addr: Option<SocketAddr>,
    msg_item_id: u32,
}
``` [4](#0-3) 

No `peer` field. When `content.to == self_peer_id`, it retrieves stored addresses by `content.from` and spawns one async task per message via `runtime::spawn`, running all addresses concurrently via `select_ok`:

```rust
let listens_info = self.protocol.pending_delivered.get(&content.from)...
let tasks = listens.into_iter()
    .map(|listen_addr| Box::pin(try_nat_traversal(self.bind_addr, listen_addr)))
    .collect::<Vec<_>>();
...
runtime::spawn(async move {
    if let Ok(((stream, addr), _)) = select_ok(tasks).await { ... }
});
``` [5](#0-4) 

**Root cause 4 — `try_nat_traversal` runs for 30 seconds per address:**

```rust
let timeout_duration = Duration::from_secs(30);
``` [6](#0-5) 

**Rate limiter bypass:**

Three guards exist, all bypassable:

1. **Per-session rate limiter** (`mod.rs` L95–107): keyed by `(session_id, msg_item_id)` — 30 req/sec per session. This is the binding constraint. [7](#0-6) 

2. **`forward_rate_limiter`** (`connection_request.rs` L132–143, `connection_sync.rs` L85–96): keyed by `(from, to, msg_item_id)` — fully bypassed by rotating `from` peer IDs. [8](#0-7) 

3. **`HOLE_PUNCHING_INTERVAL`** (`connection_request.rs` L161–167): 2-minute cooldown per `from_peer_id` — fully bypassed by rotating `from` peer IDs. [9](#0-8) 

**`pending_delivered` map is unbounded between cleanup intervals:**

Cleanup runs in `notify()` every `CHECK_INTERVAL` = 5 minutes. With 30 inserts/sec, the map grows to ~9,000 entries between cleanups. [10](#0-9) 

**Exploit arithmetic:**

- 30 `ConnectionRequest`/sec × 24 addresses each → 30 new `pending_delivered` entries/sec
- 30 `ConnectionSync`/sec → 30 spawned tasks/sec, each running 24 concurrent TCP attempts via `select_ok`
- Each task lives up to 30 seconds → **900 concurrent tasks at steady state**, each with 24 concurrent TCP connection attempts = **21,600 concurrent TCP attempts**

## Impact Explanation

This is a **High** severity vulnerability matching: *"Vulnerabilities which could easily crash a CKB node"* (10001–15000 points).

Spawning 900 concurrent 30-second async tasks from a single P2P connection, each driving 24 concurrent TCP connection attempts, exhausts the Tokio runtime's task capacity and the process's file-descriptor limit (default Linux: 1024–65536 FDs). The victim node becomes unresponsive or crashes. Multiple attacker connections multiply the effect linearly. The node is taken fully offline, disrupting block propagation and transaction relay.

## Likelihood Explanation

Preconditions are minimal: a standard P2P connection to the victim (no special privileges) and the victim's peer ID (publicly discoverable via DHT/peer exchange). The two-message sequence is trivial to craft. The attacker only needs to generate fresh random `PeerId` bytes for each `from` field — syntactic validity is the only requirement (`PeerId::from_bytes` at `connection_request.rs` L36–38). [11](#0-10) 

The attack is repeatable, automatable, and effective from a single connection.

## Recommendation

1. **Reject private/loopback/link-local addresses** in `respond_delivered()` before inserting into `pending_delivered`. Add a filter on `Protocol::Ip4(addr)` checking `addr.is_private() || addr.is_loopback() || addr.is_link_local()` and equivalent IPv6 checks.
2. **Bind `pending_delivered` to the actual session peer ID**, not `content.from`. Resolve the session peer ID from `self.peer` (already available in `ConnectionRequestProcess`) and use it as the map key.
3. **Verify the `ConnectionSync` sender** by passing the session peer ID into `ConnectionSyncProcess` and asserting it matches the `content.from` lookup key.
4. **Cap concurrent `try_nat_traversal` tasks** with a semaphore or bounded task pool to limit blast radius even if other guards are bypassed.
5. **Bound the `pending_delivered` map size** (e.g., cap at `ADDRS_COUNT_LIMIT` entries) to prevent unbounded memory growth.

## Proof of Concept

```
1. Attacker establishes a standard P2P connection to victim.

2. For i in 1..30 (per second, within rate limit):
   Send ConnectionRequest {
     from: PeerId::random(),   // fresh ID each iteration
     to:   <victim_peer_id>,
     max_hops: 6,
     route: [],
     listen_addrs: [
       /ip4/192.168.1.1/tcp/8114,
       /ip4/10.0.0.1/tcp/22,
       /ip4/169.254.169.254/tcp/80,
       ... (24 addresses total)
     ]
   }
   // victim stores 24 addresses in pending_delivered[PeerId::random()]

3. For i in 1..30 (per second):
   Send ConnectionSync {
     from: <same PeerId used in step 2, iteration i>,
     to:   <victim_peer_id>,
     route: []
   }
   // victim spawns 1 task with 24 concurrent try_nat_traversal futures × 30 = 900 tasks/sec

4. After ~30 seconds: ~900 concurrent tasks, each with 24 concurrent TCP attempts
   = 21,600 concurrent TCP connections on victim's Tokio runtime.
   Node FD limit exhausted → unresponsive / crash.

Verification: monitor victim's process FD count and memory;
both grow linearly until crash. A unit test can mock the rate limiter
and assert spawned task count exceeds a threshold after N message pairs.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L132-143)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
        {
            debug!(
                "from: {}, to {}, item_name: {}, rate limit is reached",
                content.from, content.to, "ConnectionRequest",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionRequest");
        }
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-162)
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
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L65-66)
```rust
    let timeout_duration = Duration::from_secs(30);
    let start_time = Instant::now();
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

**File:** network/src/protocols/hole_punching/mod.rs (L173-175)
```rust
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```
