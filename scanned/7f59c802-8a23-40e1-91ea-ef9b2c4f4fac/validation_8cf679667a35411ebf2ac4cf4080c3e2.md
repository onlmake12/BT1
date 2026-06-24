Audit Report

## Title
Unauthenticated `from` Field in `ConnectionRequest` Enables `pending_delivered` Poisoning via Identity Spoofing — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary

`ConnectionRequestProcess::execute` parses `content.from` directly from the wire message and writes it as the key into `pending_delivered` without ever verifying it matches the actual `PeerId` of the sending session. Any peer with a single authenticated P2P connection can set `content.from` to an arbitrary victim `PeerId` and store attacker-controlled listen addresses under that key. When a subsequent `ConnectionSync` arrives for that victim, the target node initiates NAT traversal to the attacker's endpoints instead of the victim's real addresses.

## Finding Description

In `TryFrom<&packed::ConnectionRequestReader<'_>>` for `RequestContent`, `from` is decoded from raw message bytes with no cross-check against the session:

```rust
// connection_request.rs L36-38
let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
    StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
})?;
```

The process struct holds only a `PeerIndex` (session ID), not the session's `PeerId`:

```rust
// connection_request.rs L85-91
pub(crate) struct ConnectionRequestProcess<'a> {
    peer: PeerIndex,   // session ID only
    ...
}
```

In `execute`, after structural checks (address count, `max_hops`, route length, loop detection, rate limiting), when `self_peer_id == &content.to`, `respond_delivered` is called with the attacker-controlled `content.from` and `content.listen_addrs`:

```rust
// connection_request.rs L145-147
if self_peer_id == &content.to {
    self.respond_delivered(content.from, &content.to, content.listen_addrs).await
```

Inside `respond_delivered`, the attacker-controlled `from_peer_id` and `remote_listens` are written unconditionally into `pending_delivered`:

```rust
// connection_request.rs L234-237
let now = unix_time_as_millis();
self.protocol
    .pending_delivered
    .insert(from_peer_id, (remote_listens, now));
```

At no point is the session's actual `PeerId` looked up from the peer registry (via `get_peer(session_id)`) and compared against `content.from`. The `peer` field is used only to route the `ConnectionRequestDelivered` reply back to the sender session (`send_message_to(self.peer, ...)`).

`pending_delivered` is consumed by `ConnectionSyncProcess::execute`. When a `ConnectionSync` arrives with `from=victim_peer_id`, the target looks up `pending_delivered.get(&content.from)` and initiates NAT traversal to whatever addresses are stored:

```rust
// connection_sync.rs L111-115
let listens_info = self
    .protocol
    .pending_delivered
    .get(&content.from)
    .map(|info| info.0.clone());
```

It then calls `control.raw_session(stream, addr, ...)` to the poisoned addresses:

```rust
// connection_sync.rs L154-160
let _ignore = control
    .raw_session(
        stream,
        addr,
        RawSessionInfo::inbound(listen_addr),
    )
    .await;
```

**Existing guards are insufficient:**

- The `HOLE_PUNCHING_INTERVAL` (2-minute cooldown) is keyed by `from_peer_id` — rotating through different victim `PeerId` values bypasses it entirely.
- The `forward_rate_limiter` is keyed by `(from, to, item_id)` — varying any of the three fields bypasses it.
- The per-session `rate_limiter` keyed by `(session_id, msg.item_id)` only caps the raw message rate per session, not the number of distinct victim `PeerId` entries that can be poisoned.

## Impact Explanation

The concrete impact is **address poisoning in `pending_delivered`** combined with forced NAT traversal to attacker-controlled endpoints. An attacker can systematically poison entries for all well-known honest peers, causing the target to exhaust its hole-punching connection budget on attacker-controlled endpoints. This constitutes a targeted disruption of the hole-punching path with minimal cost (one P2P connection, many spoofed `from` values), matching the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**. If the attacker successfully fills the target's connection slots via repeated `raw_session` completions, this escalates toward an eclipse attack enabling consensus deviation.

## Likelihood Explanation

The attacker requires only a single authenticated P2P connection to the target (or any relay node with a path to the target). No special privileges, no leaked keys, no majority hashpower. The `ConnectionRequest` message is a standard production P2P message. The spoofed `from` field passes all existing validation because the only structural check on `from` is that it decodes as a valid `PeerId`. The attack is repeatable: the attacker rotates through arbitrary victim `PeerId` values to bypass the per-`from_peer_id` cooldown, and can also send the triggering `ConnectionSync` messages itself.

## Recommendation

In `ConnectionRequestProcess::execute` (or in `received` before dispatch), look up the session's actual `PeerId` from the peer registry using `self.peer` (the `PeerIndex`) and assert it equals `content.from`. Reject (and optionally ban) the session if they differ. The peer registry already supports this via `get_peer(session_id)` in `peer_registry.rs`. For forwarded messages (where `from` is a remote originator, not the immediate sender), enforce the `from == session PeerId` invariant only for the **first hop** (i.e., when `route` is empty), since relay nodes legitimately forward messages on behalf of the original sender.

## Proof of Concept

```
1. Attacker node (PeerId=A) establishes a P2P connection to target node T.

2. Attacker sends a ConnectionRequest message:
     from          = victim_peer_id (B, any known PeerId)
     to            = T's own PeerId
     listen_addrs  = [attacker_ip:attacker_port/p2p/B]
     max_hops      = 1
     route         = []

3. T's ConnectionRequestProcess::execute sees self_peer_id == content.to,
   calls respond_delivered(B, T, [attacker_addr]).

4. respond_delivered inserts: pending_delivered[B] = ([attacker_addr], now).

5. Attacker sends ConnectionSync{from=B, to=T} (route=[]).
   ConnectionSyncProcess::execute looks up pending_delivered[B],
   gets [attacker_addr], calls try_nat_traversal(bind_addr, attacker_addr).

6. Attacker's node accepts the raw TCP connection and completes the P2P
   handshake. T has now consumed a connection slot on an attacker-controlled
   endpoint.

7. Repeat step 2–6 with different victim PeerId values (B1, B2, …) to
   bypass the HOLE_PUNCHING_INTERVAL cooldown and exhaust T's connection
   budget.

Verification: after step 4, assert pending_delivered[B] == [attacker_addr]
in a unit test by constructing a mock ConnectionRequest with a mismatched
from field and confirming the map entry is written without error.
```