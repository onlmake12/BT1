Now I have a complete picture of the vulnerability. Let me verify the `inflight_requests` removal path as well.

### Title
Unverified Message-Body Peer IDs Allow Any Connected Peer to Exhaust `forward_rate_limiter` Tokens for Arbitrary Peer Pairs, Causing Hole-Punching DoS — (`network/src/protocols/hole_punching/mod.rs`)

---

### Summary

The `HolePunching` protocol's `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)` values taken directly from the message payload, with no verification that the actual TCP session sender matches the claimed `from` peer ID. Any connected peer can send `ConnectionRequest` or `ConnectionRequestDelivered` messages with arbitrary `from`/`to` peer IDs, consuming the forwarding rate-limit token for any peer pair. This prevents legitimate hole-punching requests between those peers from being relayed, causing a targeted DoS on NAT traversal.

---

### Finding Description

`HolePunching` maintains two rate limiters:

1. `rate_limiter: RateLimiter<(PeerIndex, u32)>` — keyed by the actual transport-layer session ID. This correctly limits per-sender throughput.
2. `forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>` — keyed by `(content.from, content.to, msg_item_id)` from the **message body**. This is the vulnerable one. [1](#0-0) 

The outer `rate_limiter` check (line 95–107) uses `session_id` from the transport layer and correctly bounds the attacker to 30 messages/second per session: [2](#0-1) 

However, inside `ConnectionRequestProcess::execute()`, the `forward_rate_limiter` is checked using `content.from` and `content.to` — values parsed from the message payload without any verification that the actual sender session corresponds to `content.from`: [3](#0-2) 

The same pattern is repeated in `ConnectionRequestDeliveredProcess::execute()`: [4](#0-3) 

The `forward_rate_limiter` allows exactly **1 request per second** per `(from, to, msg_type)` key: [5](#0-4) 

Because `content.from` and `content.to` are parsed from the wire message with no cross-check against the actual session, an attacker can craft messages with `from=peer_A, to=peer_B` for any known peer IDs, consuming the forwarding token for that pair. When peer A legitimately sends a `ConnectionRequest` destined for peer B through this relay node, the `forward_rate_limiter` rejects it with `TooManyRequests`, silently dropping the hole-punching attempt.

A secondary consequence exists in `ConnectionRequestDeliveredProcess::execute()`: when `content.from` equals the local node's own peer ID (which is public), the code unconditionally calls `self.protocol.inflight_requests.remove(&content.to)`, destroying a legitimate in-flight request record and causing the real response to be silently ignored as "not in flight": [6](#0-5) 

---

### Impact Explanation

An attacker with a single connected session can DoS the hole-punching relay function for up to 30 distinct `(peer_A, peer_B)` pairs simultaneously (bounded by the outer 30 req/sec per-session limiter). Peer IDs are publicly advertised via the discovery protocol, so the attacker can enumerate targets. Affected peers will silently fail to establish NAT traversal connections through the victim relay node, degrading network connectivity for nodes behind NAT. The `forward_rate_limiter` uses `HashMapStateStore`, which also grows unboundedly as the attacker introduces new unique `(from, to)` pairs, adding a secondary memory-exhaustion vector.

---

### Likelihood Explanation

Any peer that can establish a P2P connection to a CKB node (i.e., any unprivileged network participant) can trigger this. No special privileges, keys, or majority hashpower are required. Peer IDs are public. The attacker needs only to maintain 1 spoofed message per second per targeted pair, which is well within the outer 30 req/sec budget. The hole-punching protocol is active on nodes with `reuse_port_on_linux` or NAT traversal enabled.

---

### Recommendation

Validate that `content.from` matches the actual session's peer ID before using it as a rate-limit key. The session's authenticated peer ID is available via the peer registry:

```rust
// In received(), resolve the actual peer ID from session_id before dispatching
let actual_peer_id = self.network_state
    .with_peer_registry(|reg| reg.get_peer(session_id).map(|p| p.connected_addr.clone()));
```

Then, inside `ConnectionRequestProcess::execute()` and `ConnectionRequestDeliveredProcess::execute()`, reject the message if `content.from != actual_peer_id_of_session`. Alternatively, key the `forward_rate_limiter` on `(actual_session_peer_id, to, msg_item_id)` rather than the unverified `content.from`, consistent with how the outer `rate_limiter` uses `session_id`.

---

### Proof of Concept

```
Attacker (peer E) connects to relay node R.
Victim peer A is attempting hole-punching to peer B through R.

1. Attacker sends ConnectionRequest { from=peer_A, to=peer_B, ... } to R.
   → R's forward_rate_limiter consumes the 1 req/sec token for (peer_A, peer_B, ConnectionRequest_id).

2. Peer A sends its legitimate ConnectionRequest { from=peer_A, to=peer_B, ... } to R.
   → R's forward_rate_limiter.check_key((peer_A, peer_B, ...)) returns Err (rate exceeded).
   → R returns TooManyRequests and drops the message silently.
   → Peer B never receives the hole-punching request; NAT traversal fails.

Attacker repeats step 1 once per second to maintain the DoS.
With 30 req/sec budget, attacker can simultaneously block 30 distinct (from, to) pairs.
```

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L45-46)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L150-176)
```rust
                let self_peer_id = self.protocol.network_state.local_peer_id();
                if self_peer_id != &content.from {
                    // forward the message to the `from` peer
                    self.forward_delivered(&content.from).await
                } else {
                    // the current peer is the target peer, respond the sync back
                    if let Some(metrics) = ckb_metrics::handle() {
                        metrics.ckb_hole_punching_active_count.inc();
                    }

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
                    }
```
