Audit Report

## Title
Unauthenticated `from` Field in `ConnectionRequestDelivered` Enables Inflight-Request Drain and Attacker-Directed NAT Traversal — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

## Summary
Any connected peer can send a crafted `ConnectionRequestDelivered` message with `from` set to the victim node's own peer ID and `route` set to empty. Because `execute()` only checks `content.from == local_peer_id` without verifying that `content.from` matches the actual sender's session identity, the attacker enters the terminal branch, drains `inflight_requests` entries, and triggers `try_nat_traversal` against attacker-supplied addresses. Repeated invocations spawn unbounded 30-second TCP connection loops, exhausting file descriptors and connection slots on the victim node.

## Finding Description

**Root cause — no sender authentication:**

`execute()` resolves the routing decision solely by comparing `content.from` (a wire-supplied field) against the local peer ID: [1](#0-0) 

`self.peer` (the actual `PeerIndex` of the sending session) is never resolved to a `PeerId` and never compared against `content.from`. It is only used inside `respond_sync` to echo a message back to the actual session: [2](#0-1) 

**Exploit path:**

An attacker connected as peer A sends:
```
ConnectionRequestDelivered {
    from:         victim_local_peer_id,   // spoofed
    to:           peer_B_id,              // any key in inflight_requests
    route:        [],                     // empty → None branch
    listen_addrs: [/ip4/1.2.3.4/tcp/9999/p2p/<peer_B_id>],
    sync_route:   [],
}
```

With `route = []`, `content.route.last()` returns `None`. Since `content.from == local_peer_id`, the `else` branch is entered: [3](#0-2) 

`inflight_requests.remove(&content.to)` removes the legitimate entry. If it was present, `try_nat_traversal` is called with the attacker's addresses.

**`listen_addrs` validation is insufficient:**

The only check on `listen_addrs` is that any embedded peer ID matches `content.to`: [4](#0-3) 

Since the attacker controls `content.to`, this check is trivially satisfied.

**`try_nat_traversal` resource exhaustion:**

Each call spawns an async task that loops for 30 seconds, issuing a TCP `connect()` every ~200 ms per address: [5](#0-4) 

With `ADDRS_COUNT_LIMIT = 24` addresses per message and the session-level rate limiter permitting 30 `ConnectionRequestDelivered` messages per second: [6](#0-5) 

an attacker can spawn up to 30 × 24 = 720 concurrent 30-second TCP connection tasks per second, accumulating ~21,600 live tasks within 30 seconds.

**`forward_rate_limiter` bypass:**

The forward rate limiter is keyed by `(content.from, content.to, msg_item_id)`: [7](#0-6) 

Since the attacker controls both `from` and `to`, using distinct `to` values for each message trivially bypasses the 1/sec per-pair limit.

**`inflight_requests` observability:**

`ConnectionRequest` gossip is broadcast to `sqrt(total)` peers: [8](#0-7) 

A connected attacker receives these broadcasts and can directly read the `to` peer IDs that will be inserted into `inflight_requests`.

## Impact Explanation

**High — Vulnerabilities which could easily crash a CKB node.**

Accumulating thousands of concurrent 30-second async TCP connection tasks exhausts the victim node's file descriptors and Tokio task memory. Each task holds a `TcpSocket` and loops with `runtime::delay_for`, consuming OS resources for the full 30-second window. At 720 new tasks/second, the node reaches OS file-descriptor limits (typically 1024–65535) within seconds to minutes, causing all subsequent socket operations (P2P connections, RPC, sync) to fail with `EMFILE`/`ENFILE`, effectively crashing the node's networking layer.

Secondary impact: every `inflight_requests` entry can be drained by a single crafted message, permanently suppressing legitimate hole-punching for the 5-minute `CHECK_INTERVAL` window. [9](#0-8) 

## Likelihood Explanation

The attacker requires only a standard P2P connection to the victim — no special privileges. The `from` field requires no cryptographic proof. The target `to` peer IDs are observable from gossip broadcasts received by any connected peer. The attack is repeatable indefinitely from a single session (bounded only by the 30 req/sec session rate limit) and from multiple sessions simultaneously with no additional cost.

## Recommendation

In `execute()`, before entering the `inflight_requests.remove` branch, resolve the actual sender's `PeerId` from `self.peer` via the peer registry and assert it equals `content.from`. Reject and ban the session if they differ:

```rust
// In the None => { else { ... } } branch, before inflight_requests.remove:
let actual_sender_peer_id = self.protocol.network_state
    .peer_registry.read()
    .get_peer_id_by_index(self.peer);  // resolve PeerIndex → PeerId

match actual_sender_peer_id {
    Some(id) if id == content.from => { /* proceed */ }
    _ => return StatusCode::InvalidFromPeerId
             .with_context("from field does not match actual sender"),
}
```

This mirrors the existing pattern in `forward_delivered` where `self.peer` is used to look up the next hop via the peer registry. [10](#0-9) 

## Proof of Concept

```
Setup:
  victim.inflight_requests = { peer_B_id: T }   // populated by notify()
  attacker has a live P2P session to victim

Step 1 — Drain + trigger NAT traversal:
  Attacker sends ConnectionRequestDelivered {
      from:         victim_local_peer_id,
      to:           peer_B_id,
      route:        [],
      listen_addrs: [/ip4/1.2.3.4/tcp/9999/p2p/<peer_B_id>],  // attacker-controlled
      sync_route:   [],
  }

Step 2 — Resource exhaustion loop (repeat at 30 msg/sec):
  for i in 0..N:
      send ConnectionRequestDelivered {
          from: victim_local_peer_id,
          to:   fresh_peer_id_i,          // bypasses forward_rate_limiter
          route: [],
          listen_addrs: [24 × /ip4/attacker_ip/tcp/<port_i>/p2p/<fresh_peer_id_i>],
      }
      // Each message spawns 24 × 30-second TCP connect loops

Assert after ~30 seconds:
  victim file-descriptor count approaches OS limit
  victim.inflight_requests.contains(peer_B_id) == false
  TCP SYN packets observed at 1.2.3.4:9999
  victim P2P/RPC connections begin failing with EMFILE
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L57-63)
```rust
                        if let Some(peer_id) = extract_peer_id(&addr) {
                            if peer_id != to {
                                return Err(StatusCode::InvalidListenAddrLen
                                    .with_context("peer id in listen address is invalid"));
                            }
                        } else {
                            addr.push(Protocol::P2P(Cow::Borrowed(to.as_bytes())));
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L134-145)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
        {
            debug!(
                "from: {}, to {}, item_name: {}, rate limit is reached",
                content.from, content.to, "ConnectionRequestDelivered",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionRequestDelivered");
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L150-154)
```rust
                let self_peer_id = self.protocol.network_state.local_peer_id();
                if self_peer_id != &content.from {
                    // forward the message to the `from` peer
                    self.forward_delivered(&content.from).await
                } else {
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L159-175)
```rust

                    let request_start = self.protocol.inflight_requests.remove(&content.to);

                    match request_start {
                        Some(start) => {
                            let res = self.respond_sync(content.from).await;
                            if !res.is_ok() {
                                return res;
                            }
                            let now = unix_time_as_millis();
                            let ttl = now - start;

                            self.try_nat_traversal(ttl, content.listen_addrs);

                            Status::ok()
                        }
                        None => StatusCode::Ignore.with_context("the request is not in flight"),
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L183-188)
```rust
        let target_sid = self
            .protocol
            .network_state
            .peer_registry
            .read()
            .get_key_by_peer_id(peer_id);
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L226-229)
```rust
        if let Err(error) = self
            .p2p_control
            .send_message_to(self.peer, proto_id, new_message)
            .await
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L65-68)
```rust
    let timeout_duration = Duration::from_secs(30);
    let start_time = Instant::now();
    let mut retry_count = 0u32;
    while start_time.elapsed() < timeout_duration {
```

**File:** network/src/protocols/hole_punching/mod.rs (L25-25)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
```

**File:** network/src/protocols/hole_punching/mod.rs (L223-235)
```rust
                    // Broadcast to a number of nodes equal to the square root of the total connection count using gossip.
                    let mut total = status.total.isqrt();
                    let _ignore = context
                        .filter_broadcast(
                            TargetSession::Filter(Box::new(move |_| {
                                total = total.saturating_sub(1);
                                total != 0
                            })),
                            proto_id,
                            conn_req.as_bytes(),
                        )
                        .await;
                    inflight.push(to_peer_id);
```

**File:** network/src/protocols/hole_punching/mod.rs (L251-252)
```rust
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```
