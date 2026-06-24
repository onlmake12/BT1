The code matches the claims. Let me verify the rate limiter and session-level guard details before concluding.

Audit Report

## Title
Unauthenticated `from` Field in `ConnectionRequestDelivered` Enables Inflight-Request Drain and Attacker-Directed NAT Traversal — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

## Summary

The `execute()` method in `ConnectionRequestDeliveredProcess` routes into the terminal branch — which removes entries from `inflight_requests` and spawns outbound TCP connection loops — based solely on whether `content.from` equals the local peer ID. Because `content.from` is parsed directly from the wire message with no check against the actual sender's session identity (`self.peer`), any connected peer can spoof `from = local_peer_id` to trigger this branch, drain inflight hole-punch state, and cause the victim node to make repeated TCP connections to attacker-supplied addresses.

## Finding Description

**Root cause:** `content.from` is deserialized from the wire message at line 38–40 of `connection_request_delivered.rs`. The terminal branch at line 151 is gated only on `self_peer_id != &content.from`, where `self_peer_id` is the victim's own local peer ID. The actual sender's identity (`self.peer`, a `PeerIndex`) is never resolved to a `PeerId` and never compared against `content.from`.

**Exploit path:**

1. Attacker establishes a standard P2P connection to the victim.
2. Attacker observes `ConnectionRequest` gossip (broadcast to `sqrt(total)` peers at lines 223–234 of `mod.rs`) to learn a peer ID present in `inflight_requests`.
3. Attacker sends a crafted `ConnectionRequestDelivered` with:
   - `from = victim_local_peer_id` (known from the connection handshake)
   - `to = observed_peer_id` (from gossip)
   - `route = []` (empty, to reach the terminal branch)
   - `listen_addrs = [/ip4/<attacker_ip>/tcp/<port>/p2p/<observed_peer_id>]`
4. At line 151, `self_peer_id == &content.from` is true → enters the `else` branch.
5. Line 160: `self.protocol.inflight_requests.remove(&content.to)` removes the legitimate entry and returns `Some(start)`.
6. Line 164: `respond_sync(content.from)` sends a sync response back to the attacker's actual session (`self.peer`, line 228).
7. Line 171: `self.try_nat_traversal(ttl, content.listen_addrs)` spawns an async task that loops for up to 30 seconds, issuing TCP `connect()` calls every ~200 ms to each attacker-supplied address (up to `ADDRS_COUNT_LIMIT = 24`).
8. If any attacker-controlled endpoint accepts the connection, line 274–282 calls `control.raw_session()`, establishing a full P2P session.

**Why existing guards fail:**

- The session-level `rate_limiter` (lines 95–107 of `mod.rs`) is keyed by `(session_id, msg.item_id())` and allows 30 messages/second — it does not prevent the exploit, only bounds its rate.
- The `forward_rate_limiter` (lines 134–145 of `connection_request_delivered.rs`) is keyed by `(content.from, content.to, msg_item_id)`. Since the attacker controls both `from` and `to`, using distinct `to` values (one per `inflight_requests` entry) bypasses this limiter entirely.
- The `listen_addrs` validation (lines 56–70) only checks that any embedded peer ID matches `content.to`, which the attacker also controls.
- No `StatusCode` in the 4xx range (which triggers a ban) is returned for this path; the node silently processes the spoofed message.

## Impact Explanation

**Inflight-request drain:** Every entry in `inflight_requests` can be removed by a single crafted message per entry. This permanently suppresses legitimate hole-punching attempts for the current 5-minute `CHECK_INTERVAL` cycle, constituting a targeted DoS against the hole-punching subsystem.

**Resource exhaustion / node crash:** At 30 messages/second (session rate limit), each carrying up to 24 addresses, the victim spawns up to 720 new async tasks per second, each holding open TCP sockets for up to 30 seconds. After 30 seconds of sustained attack: 30 × 30 × 24 = 21,600 concurrent async tasks, each consuming a file descriptor. This can exhaust the OS file descriptor limit and crash the CKB node process.

**Unauthorized raw session establishment:** If the attacker controls a listening endpoint, `control.raw_session()` is called (lines 274–282), establishing a full P2P session to an attacker node under the guise of a legitimate hole-punch, bypassing normal connection admission logic.

This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

The attacker requires only a standard P2P connection to the victim — no special privileges. The victim's local peer ID is learned during the connection handshake. Target peer IDs in `inflight_requests` are observable from `ConnectionRequest` gossip broadcast to `sqrt(total)` peers (lines 223–234 of `mod.rs`), so a connected attacker receives them directly. The attack is repeatable every 5-minute `notify()` cycle and requires no cryptographic material.

## Recommendation

In `execute()`, before entering the `inflight_requests.remove` branch, resolve the actual sender's peer ID from `self.peer` via the peer registry (analogous to how `forward_delivered` resolves peer IDs at lines 183–188) and assert it equals `content.from`. If they differ, return a `StatusCode` in the 4xx range to trigger a session ban. This mirrors the existing pattern used in `forward_delivered` where session identity is cross-checked against message fields.

## Proof of Concept

```
Pre-condition:
  victim.inflight_requests = { peer_B_id: T }   // populated by notify() every 5 min
  attacker has a live P2P session to victim (session_id = S)
  attacker knows victim_local_peer_id (from handshake)
  attacker observed peer_B_id from ConnectionRequest gossip

Attacker sends ConnectionRequestDelivered from session S:
  from:         victim_local_peer_id   // spoofed — never verified against S
  to:           peer_B_id              // known from gossip
  route:        []                     // empty → triggers terminal branch at line 149
  listen_addrs: [/ip4/1.2.3.4/tcp/9999/p2p/<peer_B_id>]
  sync_route:   []

Execution trace in execute():
  line 147: content.route.last() == None          → None branch
  line 151: self_peer_id == &content.from         → else branch (no sender check)
  line 160: inflight_requests.remove(peer_B_id)   → Some(T), entry drained
  line 164: respond_sync(victim_local_peer_id)    → sync sent back to attacker session S
  line 171: try_nat_traversal(ttl, [1.2.3.4:9999])→ 30-second TCP connect loop spawned

Assertions:
  victim.inflight_requests.contains_key(peer_B_id) == false
  TCP SYN packets observed at 1.2.3.4:9999 for ~30 seconds
  If 1.2.3.4:9999 accepts: raw_session() called, attacker gains P2P session

Amplification test:
  Repeat at 30 msg/sec with 24 listen_addrs each for 30 seconds:
  → 21,600 concurrent async tasks, each holding a TCP socket fd
  → file descriptor exhaustion → node crash
```