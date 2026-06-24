All cited code references are confirmed against the actual source. Let me verify the key claims:

Audit Report

## Title
Unverified Message-Body Peer IDs Allow Spoofed `forward_rate_limiter` Token Exhaustion, Causing Hole-Punching Relay DoS — (`network/src/protocols/hole_punching/mod.rs`)

## Summary
The `HolePunching` protocol's `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)` values parsed directly from the wire message, with no verification that the actual TCP session sender matches `content.from`. Any connected peer can craft `ConnectionRequest` or `ConnectionRequestDelivered` messages with arbitrary `from`/`to` peer IDs, exhausting the 1 req/sec forwarding token for any peer pair. Legitimate hole-punching requests between those peers are then silently dropped with `TooManyRequests`, causing targeted NAT traversal failures.

## Finding Description
Two rate limiters exist in `HolePunching` (`mod.rs` L45-46):
- `rate_limiter: RateLimiter<(PeerIndex, u32)>` — keyed by the transport-layer `session_id`. Correctly bounds per-sender throughput at 30 req/sec.
- `forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>` — keyed by `(content.from, content.to, msg_item_id)` from the **message body**. Vulnerable.

The outer check in `received()` (`mod.rs` L95-107) uses the authenticated `session_id` and correctly limits an attacker to 30 messages/second per session. However, inside `ConnectionRequestProcess::execute()` (`connection_request.rs` L132-143), the `forward_rate_limiter` is checked using unverified payload fields `content.from` and `content.to`. The same pattern appears in `ConnectionRequestDeliveredProcess::execute()` (`connection_request_delivered.rs` L134-145).

The `forward_rate_limiter` is configured at 1 req/sec per key (`mod.rs` L254-257). A search of the entire hole-punching component confirms there is no reverse lookup from `session_id` to `PeerId` to validate `content.from` against the actual session identity — the `peer_registry` exposes `get_key_by_peer_id` (peer ID → session) but the hole-punching code never performs the inverse to authenticate the sender.

**Primary exploit**: Attacker sends `ConnectionRequest { from=peer_A, to=peer_B }`. The outer limiter passes (1 of 30 budget). The `forward_rate_limiter` consumes the 1 req/sec token for `(peer_A, peer_B, ...)`. When peer A legitimately sends the same message, `forward_rate_limiter.check_key` returns `Err`, the relay drops it silently, and peer B never receives the hole-punching request.

**Secondary exploit** (`connection_request_delivered.rs` L150-160): When `content.route` is empty and `content.from` equals the local node's own peer ID (publicly advertised), the code unconditionally calls `self.protocol.inflight_requests.remove(&content.to)`. An attacker setting `from=local_peer_id` and `to=victim_peer_id` destroys the in-flight request record; the legitimate response is then silently ignored as `"the request is not in flight"`.

**Memory growth**: `retain_recent()` on `forward_rate_limiter` is only called in `disconnected()` (`mod.rs` L67-68). A long-lived attacker session continuously introducing new spoofed `(from, to)` pairs causes unbounded growth of the `HashMapStateStore` backing `forward_rate_limiter`.

## Impact Explanation
An attacker with a single connected session can simultaneously block hole-punching relay for up to 30 distinct `(peer_A, peer_B)` pairs (bounded by the outer 30 req/sec limiter). Peer IDs are publicly advertised via the discovery protocol. Affected nodes behind NAT silently fail to establish connections through the victim relay node. Targeting multiple relay nodes simultaneously degrades NAT traversal network-wide, preventing NAT-ed nodes from joining the CKB P2P network. This matches the High impact class: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation
Any unprivileged peer that can establish a P2P connection to a CKB node can trigger this. No keys, hashpower, or special privileges are required. Peer IDs are public. The attacker needs only to maintain 1 spoofed message/second per targeted pair, well within the 30 req/sec outer budget. The hole-punching protocol is active on nodes with `reuse_port_on_linux` enabled or NAT traversal configured.

## Recommendation
Before dispatching to `ConnectionRequestProcess` or `ConnectionRequestDeliveredProcess`, resolve the actual peer ID of the session from the peer registry (via `get_peer(session_id)` → `extract_peer_id(&peer.connected_addr)`) and pass it alongside `session_id`. Inside both `execute()` methods, reject the message if `content.from` does not match the authenticated session peer ID. Alternatively, key `forward_rate_limiter` on `(actual_session_peer_id, content.to, msg_item_id)` instead of the unverified `content.from`, consistent with how `rate_limiter` uses `session_id`. For the secondary issue, guard the `inflight_requests.remove` path by verifying the message arrived from a session whose peer ID matches `content.to` (the relay node that delivered the response), not just by comparing `content.from` to the local peer ID. Additionally, call `retain_recent()` on a periodic timer (e.g., in `notify()`) rather than only on disconnect to bound memory growth.

## Proof of Concept
```
Setup: Relay node R has hole-punching active. Peer A (behind NAT) is attempting to reach peer B through R.

1. Attacker E connects to R (establishes a valid P2P session).

2. Attacker sends: ConnectionRequest { from=peer_A, to=peer_B, listen_addrs=[valid], max_hops=6, route=[] }
   → R's outer rate_limiter passes (session E, 1 of 30 budget).
   → R's forward_rate_limiter consumes the 1 req/sec token for (peer_A, peer_B, ConnectionRequest_id).

3. Peer A sends its legitimate: ConnectionRequest { from=peer_A, to=peer_B, ... } to R.
   → R's forward_rate_limiter.check_key((peer_A, peer_B, ...)) returns Err (rate exceeded).
   → R returns TooManyRequests, drops the message silently.
   → Peer B never receives the hole-punching request; NAT traversal fails.

4. Attacker repeats step 2 once per second to maintain the DoS indefinitely.
   With 30 req/sec budget, attacker simultaneously blocks 30 distinct (from, to) pairs.

Secondary PoC (inflight_requests destruction):
5. Attacker sends: ConnectionRequestDelivered { from=R's_peer_id, to=peer_A, route=[], listen_addrs=[valid] }
   → forward_rate_limiter passes (new key).
   → content.route is empty, content.from == local_peer_id → enters else branch.
   → inflight_requests.remove(&peer_A) destroys R's legitimate in-flight record for peer_A.
   → When real response arrives, it is dropped as "the request is not in flight".
```