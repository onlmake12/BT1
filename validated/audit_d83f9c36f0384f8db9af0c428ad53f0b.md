Audit Report

## Title
Unauthenticated `content.from` in Hole-Punching Protocol Enables Arbitrary NAT Traversal and Resource Exhaustion — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

## Summary

Any peer with a single direct P2P connection to a victim node can force the victim to spawn unbounded async TCP tasks targeting attacker-controlled addresses. `ConnectionRequestProcess::execute()` never verifies that `content.from` matches the authenticated session peer ID before inserting into `pending_delivered`, and `ConnectionSyncProcess` has no `peer` field at all, making the check structurally impossible. By rotating synthetic `content.from` peer IDs, an attacker bypasses all rate limiting and interval guards, causing file descriptor exhaustion and unauthorized inbound P2P sessions via `raw_session()`.

## Finding Description

**Root cause:** `content.from` is a message field freely set by the sender and is never compared against the actual authenticated session peer ID at any point in either `ConnectionRequestProcess` or `ConnectionSyncProcess`.

**Step 1 — Populate `pending_delivered` with attacker addresses:**

`ConnectionRequestProcess::execute()` receives the actual session as `self.peer: PeerIndex` but never checks `content.from` against it. [1](#0-0) 

When `self_peer_id == &content.to`, `respond_delivered()` is called with the attacker-controlled `content.from` as the key. After filtering for TCP/IPv4/IPv6, the attacker's listen addresses are stored unconditionally: [2](#0-1) 

**Step 2 — Trigger NAT traversal:**

`ConnectionSyncProcess` has no `peer` field whatsoever: [3](#0-2) 

With `route: []`, the `None` branch is taken. After confirming `self_peer_id == &content.to`, the code reads `pending_delivered` using the attacker-controlled `content.from` key with no session identity check: [4](#0-3) 

This yields the attacker's addresses, and `try_nat_traversal` is spawned for each. On TCP success, `raw_session()` is called: [5](#0-4) 

**Why existing guards fail:**

- `forward_rate_limiter` is keyed on `(content.from, content.to, msg_item_id)`. Since `content.from` is attacker-controlled and never verified, rotating fake peer IDs creates fresh rate-limiter buckets, bypassing the 1 req/sec limit entirely: [6](#0-5) 

- `HOLE_PUNCHING_INTERVAL` in `respond_delivered()` only prevents re-inserting the same `from_peer_id` within 2 minutes — trivially bypassed by rotating fake peer IDs: [7](#0-6) 

- `pending_delivered` entries are never consumed after `ConnectionSync` processes them; they persist for up to 5 minutes, enabling replay: [8](#0-7) 

- The only effective cap is the per-session `rate_limiter` at 30 msg/sec per message type, meaning an attacker can send 30 `ConnectionRequest` + 30 `ConnectionSync` pairs per second per session: [9](#0-8) 

## Impact Explanation

**High — Vulnerabilities which could easily crash a CKB node / cause CKB network congestion with few costs.**

At 30 pairs/sec per session, each pair spawns an async task that opens a TCP connection. Even failed TCP attempts consume file descriptors and async task pool slots. Successful connections (when the attacker runs a listening server) call `raw_session()`, injecting unauthorized inbound P2P sessions that bypass peer scoring, connection limits, and peer selection — permanently occupying connection slots. With multiple sessions, the victim's file descriptors, async task pool, and TCP connection slots are exhausted, crashing the node.

## Likelihood Explanation

Any peer with a single authenticated P2P connection to the victim can execute this attack. No special privileges, leaked keys, or majority hashpower are required. The `content.from` field is never cryptographically bound to the session identity. The attack is repeatable at up to 30 message pairs/second per session and scales linearly with the number of sessions the attacker can open. Rotating fake peer IDs is trivial and costs nothing.

## Recommendation

1. In `ConnectionRequestProcess::execute()`, resolve the actual peer ID of `self.peer` from the peer registry and verify it equals `content.from` before calling `respond_delivered()`. Reject with `StatusCode::Ignore` if they differ.
2. Add a `peer: PeerIndex` field to `ConnectionSyncProcess` and perform the same check: resolve the session peer ID and verify it equals `content.from` before reading `pending_delivered`.
3. Remove the `pending_delivered` entry after it is consumed by a `ConnectionSync` to prevent replay attacks.
4. The correct invariant: a `ConnectionSync` should only trigger NAT traversal if `content.from` equals the authenticated peer ID of the actual sender **and** a `ConnectionRequestDelivered` was already sent for that exact `(from, to)` pair.

## Proof of Concept

```
Setup: Attacker (peer A) has one direct P2P connection to victim V.

Round 1:
  A → V: ConnectionRequest {
    from: fake_PeerID_1,   // arbitrary, never verified against session
    to: V_peer_id,
    listen_addrs: [attacker_server:1234],
    route: [],
    max_hops: 6
  }
  V: self_peer_id == content.to → respond_delivered()
     → pending_delivered.insert(fake_PeerID_1, ([attacker_server:1234], now))

  A → V: ConnectionSync {
    from: fake_PeerID_1,   // matches key inserted above
    to: V_peer_id,
    route: []
  }
  V: route.last() == None, self_peer_id == content.to
     → pending_delivered.get(&fake_PeerID_1) → Some([attacker_server:1234])
     → runtime::spawn(try_nat_traversal(bind_addr, attacker_server:1234))
     → on success: control.raw_session(...) → unauthorized inbound P2P session

Round 2..N (rotate fake_PeerID_2, fake_PeerID_3, ...):
  Each new fake_PeerID bypasses forward_rate_limiter and HOLE_PUNCHING_INTERVAL.
  Repeat at 30 pairs/sec per session.
  → async task pool exhaustion, file descriptor exhaustion → node crash
  → each successful raw_session() occupies a connection slot permanently
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
