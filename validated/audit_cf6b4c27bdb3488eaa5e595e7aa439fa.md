Audit Report

## Title
Unauthenticated `ConnectionSync` Bypasses Three-Way Handshake, Triggering NAT Traversal to Attacker-Controlled Addresses — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

## Summary

Any peer with a direct P2P connection to a victim node can force the victim to initiate outbound TCP connections to arbitrary attacker-controlled addresses by sending two sequential hole-punching messages. Because `ConnectionSyncProcess::execute()` never verifies that `content.from` matches the actual session peer ID, an attacker can populate `pending_delivered` with attacker-controlled addresses via `ConnectionRequest`, then immediately trigger NAT traversal via `ConnectionSync`. If the TCP connection succeeds, the victim establishes an unauthorized raw inbound P2P session with the attacker's server.

## Finding Description

The hole-punching protocol intends a three-phase relay-mediated handshake, but the final phase (`ConnectionSync`) has no guard verifying that the relay path was actually traversed.

**Step 1 — Populate `pending_delivered` with attacker addresses:**

In `connection_request.rs`, `execute()` checks `self_peer_id == &content.to` at line 145. The attacker sets `content.to` to the victim's peer ID, so this passes and `respond_delivered()` is called. [1](#0-0) 

Inside `respond_delivered()`, after filtering for TCP/IPv4/IPv6 addresses, the attacker-supplied `listen_addrs` are stored unconditionally: [2](#0-1) 

**Step 2 — Trigger NAT traversal immediately:**

In `connection_sync.rs`, `execute()` with `route=[]` takes the `None` branch, confirms `self_peer_id == &content.to`, then reads `pending_delivered` using the attacker-controlled `content.from` key: [3](#0-2) 

Since `pending_delivered[A]` was populated in Step 1, `listens_info` is `Some(attacker_addrs)`. The victim spawns `try_nat_traversal` to those addresses and, on success, calls `control.raw_session()`: [4](#0-3) 

**Why existing guards fail:**

- The `forward_rate_limiter` is keyed on `(content.from, content.to, msg_item_id)`. Since `content.from` is entirely attacker-controlled and never verified against the actual session peer ID, the attacker can use a fresh synthetic `from` peer ID for each pair of messages, bypassing the 1 req/sec limit entirely. [5](#0-4) 

- The `HOLE_PUNCHING_INTERVAL` check in `respond_delivered()` only prevents re-inserting the same `from_peer_id` within 2 minutes — trivially bypassed by rotating fake peer IDs. [6](#0-5) 

- `pending_delivered` entries persist for up to 5 minutes and are never consumed/removed after a `ConnectionSync` processes them, enabling replay. [7](#0-6) 

- There is no check anywhere in `ConnectionSyncProcess` that the actual session peer ID matches `content.from`. [8](#0-7) 

## Impact Explanation

**High — Vulnerabilities which could easily crash a CKB node / cause CKB network congestion with few costs.**

By rotating synthetic `from` peer IDs, the attacker bypasses all rate limiting and can flood the victim with `(ConnectionRequest, ConnectionSync)` pairs at the per-session cap of 30 msg/sec. Each pair causes the victim to spawn an async task that opens a TCP connection and, on success, calls `raw_session()`. This exhausts the victim's file descriptors, async task pool, and TCP connection slots, crashing the node. Additionally, each successful `raw_session()` injects an unauthorized inbound P2P session that bypasses normal peer scoring, connection limits, and peer selection, allowing the attacker to permanently occupy connection slots.

## Likelihood Explanation

Any peer with a single direct P2P connection to the victim can execute this attack. No special privileges, leaked keys, or majority hashpower are required. The `from` field is never cryptographically bound to the actual session identity. The attack is repeatable at up to 30 message pairs/second per session, and scales with the number of sessions the attacker can open.

## Recommendation

In `ConnectionSyncProcess::execute()`, before reading `pending_delivered`, verify that the actual session peer ID (resolved from `self.peer` / `context.session.id` via the peer registry) matches `content.from`. Reject the message if they differ. Additionally, remove the `pending_delivered` entry after it is consumed by a `ConnectionSync` to prevent replay attacks. The correct invariant is: a `ConnectionSync` should only trigger NAT traversal if `content.from` equals the authenticated peer ID of the sender **and** a `ConnectionRequestDelivered` was already sent back through the relay path for that exact `(from, to)` pair.

## Proof of Concept

```
Setup: Attacker (peer A) has one direct P2P connection to victim V.

Round 1:
  A → V: ConnectionRequest { from: fake_PeerID_1, to: V, listen_addrs: [attacker_server:1234], route: [], max_hops: 6 }
  V: self_peer_id == content.to → respond_delivered()
     → pending_delivered.insert(fake_PeerID_1, ([attacker_server:1234], now))

  A → V: ConnectionSync { from: fake_PeerID_1, to: V, route: [] }
  V: route.last() == None, self_peer_id == content.to
     → pending_delivered.get(&fake_PeerID_1) → Some([attacker_server:1234])
     → runtime::spawn(try_nat_traversal(bind_addr, attacker_server:1234))
     → on success: control.raw_session(stream, addr, RawSessionInfo::inbound(...))
     → unauthorized inbound P2P session established

Round 2..N (rotate fake_PeerID_2, fake_PeerID_3, ...):
  Repeat at 30 pairs/sec, each spawning a new async TCP task.
  → file descriptor exhaustion → node crash
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L161-166)
```rust
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
            }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L76-115)
```rust
    pub(crate) async fn execute(self) -> Status {
        let content = match SyncContent::try_from(&self.message) {
            Ok(content) => content,
            Err(status) => return status,
        };

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
            None => {
                let self_peer_id = self.protocol.network_state.local_peer_id();
                if self_peer_id != &content.to {
                    // forward the message to the `to` peer
                    self.forward_sync(&content.to).await
                } else {
                    // Current node should be the `to` target.
                    if let Some(metrics) = ckb_metrics::handle() {
                        metrics.ckb_hole_punching_passive_count.inc();
                    }

                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());
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

**File:** network/src/protocols/hole_punching/mod.rs (L173-174)
```rust
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/mod.rs (L255-257)
```rust
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```
