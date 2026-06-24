Audit Report

## Title
Unauthenticated Peer ID Spoofing Enables Arbitrary NAT Traversal via `pending_delivered` — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

## Summary

`ConnectionRequestProcess::respond_delivered` inserts attacker-controlled TCP addresses into `pending_delivered` keyed by `content.from`, a field taken verbatim from the message payload with no validation against the actual authenticated session peer (`self.peer`). `ConnectionSyncProcess` carries no session peer field at all, so when it looks up `pending_delivered[content.from]` and spawns NAT traversal tasks, there is no mechanism to verify the sender is the claimed peer. A single connected peer can therefore cause the local node to make outbound TCP connection attempts to arbitrary IP:port combinations, with the `forward_rate_limiter` trivially bypassed using fresh synthetic peer IDs.

## Finding Description

**Step 1 — Populate `pending_delivered` with attacker-controlled addresses.**

`ConnectionRequestProcess` carries the real session peer as `self.peer` (a `PeerIndex`), but `execute()` passes `content.from` — taken verbatim from the message payload — directly to `respond_delivered` without comparing it to `self.peer`:

```
// connection_request.rs L145-147
if self_peer_id == &content.to {
    self.respond_delivered(content.from, &content.to, content.listen_addrs)
        .await
```

Inside `respond_delivered`, after filtering to TCP/IPv4/IPv6 addresses only (L196–215), the attacker-supplied addresses are stored:

```
// connection_request.rs L234-237
let now = unix_time_as_millis();
self.protocol
    .pending_delivered
    .insert(from_peer_id, (remote_listens, now));
```

`from_peer_id` here is `content.from` — any syntactically valid peer ID the attacker chooses.

**Step 2 — Trigger NAT traversal from the same session.**

`ConnectionSyncProcess` is constructed without a session peer argument (mod.rs L133–143) and its struct has no `peer` field (connection_sync.rs L51–57). When `content.route` is empty and `content.to == local_peer_id`, it unconditionally looks up `pending_delivered[content.from]` and spawns traversal tasks:

```
// connection_sync.rs L111-124
let listens_info = self
    .protocol
    .pending_delivered
    .get(&content.from)
    .map(|info| info.0.clone());
// ...
let tasks = listens.into_iter()
    .map(|listen_addr| Box::pin(try_nat_traversal(self.bind_addr, listen_addr)))
    .collect::<Vec<_>>();
```

A successful traversal is promoted to a full P2P session via `control.raw_session(stream, addr, ...)` (connection_sync.rs L154–160).

**Rate-limiter analysis.**

The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)` (connection_sync.rs L85–96; connection_request.rs L132–143). Using a fresh synthetic `from` peer ID for each pair of messages produces a new key, bypassing this limiter entirely. The only remaining throttle is the per-session `rate_limiter` keyed by `(session_id, msg.item_id())` at 30 req/s (mod.rs L95–107).

The `HOLE_PUNCHING_INTERVAL` guard in `respond_delivered` (connection_request.rs L161–167) only blocks re-insertion for the *same* `from_peer_id` within 2 minutes; fresh synthetic IDs bypass it.

## Impact Explanation

A single connected peer can cause the victim node to make outbound TCP connection attempts to arbitrary IP:port combinations at up to 30 address pairs per second. Successful connections are promoted to full P2P sessions. This concretely enables:

- **Eclipse attack**: directing the victim to connect exclusively to attacker-controlled peers, isolating it from the honest network.
- **Connection slot exhaustion**: filling the outbound connection table, which can crash or severely degrade the node.
- **Unsolicited port scanning**: using the victim as a TCP probe against third-party hosts.

This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node** (connection slot exhaustion) and **High — bad designs which could cause CKB network congestion with few costs** (eclipse/isolation of nodes at scale).

## Likelihood Explanation

Any peer that can establish a single authenticated P2P session with the target node can execute this attack immediately. No special privileges, leaked keys, or majority hashpower are required. The two-message sequence (`ConnectionRequest` then `ConnectionSync`) is trivially constructable using the public protocol schema. The attacker needs only one session and can sustain the attack at 30 address pairs/second indefinitely.

## Recommendation

In `ConnectionRequestProcess::respond_delivered`, resolve `self.peer` (a `PeerIndex`) to a `PeerId` via the peer registry and reject the message if `content.from` does not match the resolved peer ID. The peer registry lookup is already used elsewhere in the same file (e.g., `forward_message` at L250–255).

In `ConnectionSyncProcess`, pass the session `PeerIndex` (as is done for `ConnectionRequestProcess` in mod.rs L114) and verify it resolves to the same `PeerId` as `content.from` before performing the `pending_delivered` lookup and spawning traversal tasks.

Additionally, consider keying `forward_rate_limiter` by `(session_id, msg_item_id)` rather than `(content.from, content.to, msg_item_id)` to prevent bypass via synthetic peer IDs.

## Proof of Concept

```
Setup: Attacker controls session B connected to victim node V.

1. Attacker sends over session B:
   ConnectionRequest {
     from = <synthetic_id_X>,   // any valid PeerId bytes, not attacker's real ID
     to   = <local_peer_id_V>,
     listen_addrs = [/ip4/1.2.3.4/tcp/9999],
     route = [],
     max_hops = 1
   }
   → V: content.to == local_peer_id → respond_delivered() called
   → forward_rate_limiter key (X, V, 1) is fresh → passes
   → TCP filter passes (/ip4/1.2.3.4/tcp/9999)
   → pending_delivered[X] = ([/ip4/1.2.3.4/tcp/9999], now)

2. Attacker sends over session B:
   ConnectionSync {
     from  = <synthetic_id_X>,
     to    = <local_peer_id_V>,
     route = []
   }
   → V: route is empty, content.to == local_peer_id
   → pending_delivered.get(X) → Some([/ip4/1.2.3.4/tcp/9999])
   → try_nat_traversal(bind_addr, /ip4/1.2.3.4/tcp/9999) spawned
   → V opens TCP connection to 1.2.3.4:9999

3. Repeat steps 1–2 with fresh synthetic_id_X' values.
   forward_rate_limiter sees new keys each time → no throttle.
   Only per-session rate_limiter applies: up to 30 pairs/second.

4. To exhaust connection slots: target 1.2.3.4:9999 … N as attacker-
   controlled peers; each successful raw_session() consumes an outbound slot.
```