The code confirms all structural claims. Let me verify the key details precisely:

All code claims check out. The vulnerability is real and exploitable. Here is my assessment:

---

Audit Report

## Title
Unauthenticated Peer-ID Spoofing Enables Arbitrary TCP NAT Traversal via `pending_delivered` — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

## Summary
`ConnectionRequestProcess::respond_delivered` inserts attacker-controlled addresses into `pending_delivered` keyed by `content.from`, a payload field never validated against the actual authenticated session peer (`self.peer`). `ConnectionSyncProcess` carries no session peer field at all, so when it looks up `pending_delivered[content.from]` it cannot perform any such check. Any single connected peer can therefore (1) populate `pending_delivered` with arbitrary TCP targets under a synthetic peer ID, then (2) trigger repeated outbound TCP connection attempts to those targets, with each spawned `try_nat_traversal` task retrying for up to 30 seconds.

## Finding Description

**Root cause — Step 1 (`ConnectionRequest`):**

`ConnectionRequestProcess` holds the real session index as `self.peer` (`PeerIndex`, line 88 of `connection_request.rs`), but `execute` passes `content.from` — taken verbatim from the message payload — directly to `respond_delivered` without comparing it to `self.peer`:

```
// connection_request.rs L145-147
if self_peer_id == &content.to {
    self.respond_delivered(content.from, &content.to, content.listen_addrs)
```

`respond_delivered` filters `listen_addrs` to TCP/IPv4/IPv6 addresses (lines 196–215), then unconditionally inserts them into `pending_delivered` keyed by the attacker-supplied `from_peer_id` (lines 234–237). The actual session peer is only used to route the acknowledgement reply back (line 228), never to authenticate `content.from`.

**Root cause — Step 2 (`ConnectionSync`):**

`ConnectionSyncProcess` has no `peer` field at all (lines 51–57 of `connection_sync.rs`). When `content.to == local_peer_id` and `content.route` is empty, `execute` looks up `pending_delivered[content.from]` (lines 111–115) and immediately spawns `try_nat_traversal` tasks for every stored address (lines 119–124), with no check that the sending session matches `content.from`.

**Why rate-limiting does not prevent the attack:**

The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)` (line 88 of `connection_sync.rs`; line 135 of `connection_request.rs`). Using a fresh synthetic `from` peer ID for each message pair bypasses this limiter entirely. The only remaining throttle is the per-session `rate_limiter` keyed by `(session_id, msg.item_id())` at 30 req/s (lines 95–107 of `mod.rs`), allowing 30 NAT traversal triggers per second per session.

**Amplification from `try_nat_traversal`:**

Each spawned task (`component/mod.rs` lines 49–115) retries TCP connections in a loop for up to 30 seconds with ~200 ms intervals, producing roughly 150 TCP SYN packets per triggered address. At 30 triggers/second, after 30 seconds there are ~900 concurrent async tasks each making repeated TCP connections, which can exhaust file descriptors and crash the node.

## Impact Explanation

**High — Vulnerabilities which could easily crash a CKB node.**

A single connected peer can spawn hundreds of concurrent long-lived async tasks, each making repeated TCP connection attempts. File descriptor exhaustion from this task flood can crash the node process. Additionally, successful `raw_session` promotions (lines 154–160 of `connection_sync.rs`) fill the outbound connection table with adversarial sessions, enabling eclipse attacks that isolate the victim from the honest network.

## Likelihood Explanation

Any peer that completes a single P2P handshake with the target node can execute this attack immediately. No special privileges, cryptographic material, or network position are required. The two-message sequence is trivially constructable from the public protocol schema. The attack is repeatable and can be sustained indefinitely within the per-session rate limit.

## Recommendation

In `ConnectionRequestProcess::respond_delivered`, resolve `self.peer` (a `PeerIndex`) to a `PeerId` via the peer registry and reject the message if it does not equal `content.from`. The peer registry lookup is already used elsewhere in the same file (e.g., `forward_message` at line 250).

In `ConnectionSyncProcess`, add a `peer: PeerIndex` field (mirroring `ConnectionRequestProcess`) and pass the session ID from `mod.rs` line 114 where `ConnectionSyncProcess::new` is called. Before performing the `pending_delivered` lookup, resolve `self.peer` to a `PeerId` and reject if it does not match `content.from`.

## Proof of Concept

```
Setup: Attacker controls session B connected to victim node V.

1. Attacker sends over session B:
   ConnectionRequest {
     from = <synthetic_id_X>,   // arbitrary bytes, not session B's real peer ID
     to   = <V's local peer ID>,
     listen_addrs = [/ip4/1.2.3.4/tcp/9999],
     route = [], max_hops = 6
   }
   → V calls respond_delivered(X, V_id, [/ip4/1.2.3.4/tcp/9999])
   → pending_delivered[X] = ([/ip4/1.2.3.4/tcp/9999], now)

2. Attacker sends over session B:
   ConnectionSync {
     from = <synthetic_id_X>,
     to   = <V's local peer ID>,
     route = []
   }
   → V finds pending_delivered[X]
   → spawns try_nat_traversal(bind_addr, /ip4/1.2.3.4/tcp/9999)
   → V opens repeated TCP connections to 1.2.3.4:9999 for 30 seconds

3. Repeat steps 1–2 with a fresh synthetic_id_X each iteration.
   The forward_rate_limiter sees a new key each time and never fires.
   The per-session rate_limiter allows 30 ConnectionRequest + 30 ConnectionSync/s.
   After 30 seconds: ~900 concurrent try_nat_traversal tasks active simultaneously.
   File descriptor exhaustion → node crash.
```