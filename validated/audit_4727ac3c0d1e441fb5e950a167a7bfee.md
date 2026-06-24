Audit Report

## Title
Unauthenticated `from` Field in `ConnectionRequest` Enables `pending_delivered` Poisoning via Identity Spoofing — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary

`ConnectionRequestProcess::execute` parses `content.from` directly from the wire message and writes it as the key into `pending_delivered` without ever verifying it against the actual `PeerId` of the sending session. Any single authenticated P2P peer can spoof an arbitrary victim `PeerId` in the `from` field, causing the target node to store attacker-controlled listen addresses under that victim's identity. A subsequent `ConnectionSync` (which the attacker can also send, since it has the same missing check) causes the target to initiate NAT traversal — up to 30 seconds of repeated TCP connection attempts — to the attacker's endpoints instead of the victim's.

## Finding Description

**Root cause**: In `connection_request.rs`, `RequestContent::try_from` parses `from` purely from message bytes with no session-identity check:

```rust
// L36-38
let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
    StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
})?;
```

The `ConnectionRequestProcess` struct holds `peer: PeerIndex` (a session ID integer), not the session's `PeerId`:

```rust
// L85-91
pub(crate) struct ConnectionRequestProcess<'a> {
    message: packed::ConnectionRequestReader<'a>,
    protocol: &'a mut HolePunching,
    peer: PeerIndex,          // session ID only
    p2p_control: &'a ServiceAsyncControl,
    msg_item_id: u32,
}
```

In `execute`, when `self_peer_id == &content.to`, `respond_delivered` is called with the wire-supplied `content.from` verbatim:

```rust
// L145-147
if self_peer_id == &content.to {
    self.respond_delivered(content.from, &content.to, content.listen_addrs)
        .await
```

Inside `respond_delivered`, the attacker-controlled `from_peer_id` and `remote_listens` are written unconditionally into `pending_delivered`:

```rust
// L234-237
let now = unix_time_as_millis();
self.protocol
    .pending_delivered
    .insert(from_peer_id, (remote_listens, now));
```

The `peer` field is only used to route the `ConnectionRequestDelivered` reply back to the sender session (L226-229). The session's actual `PeerId` is never looked up from the peer registry and compared against `content.from`.

**Exploit chain**: The attacker then sends a `ConnectionSync{from=victim_B, to=T}` (also unauthenticated — `connection_sync.rs` L42-44 has the identical missing check). `ConnectionSyncProcess::execute` looks up `pending_delivered.get(&content.from)` (L111-115) and calls `try_nat_traversal` on the stored attacker addresses (L119-124). `try_nat_traversal` (`component/mod.rs` L49-115) opens raw TCP connections with a 30-second retry loop. On success, `raw_session` is called (L154-160) with `RawSessionInfo::inbound`, consuming a connection slot.

**Why existing guards fail**:
- `rate_limiter` is keyed by `(session_id, msg.item_id)` — limits per-session message rate, not per-victim-PeerId
- `forward_rate_limiter` is keyed by `(from, to, item_id)` — attacker bypasses by rotating victim `from` PeerIds
- `HOLE_PUNCHING_INTERVAL` (2 min) prevents re-insertion for the *same* `from_peer_id` within a window, but the attacker rotates through many victim PeerIds to bypass this entirely

## Impact Explanation

**Concrete impact**: The target node stores attacker-controlled multiaddrs under arbitrary victim `PeerId` keys in `pending_delivered`. Each poisoned entry, when triggered by a `ConnectionSync`, causes the target to spawn a 30-second TCP retry loop (`try_nat_traversal`) to the attacker's infrastructure. With up to 24 addresses per entry (`ADDRS_COUNT_LIMIT`) and the attacker rotating victim PeerIds to bypass the 2-minute cooldown, the attacker can continuously exhaust the target's hole-punching connection budget and OS-level socket resources from a single P2P connection at negligible cost.

This matches the allowed bounty impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** The attacker needs only one authenticated P2P connection and can sustain the attack indefinitely by cycling victim PeerIds.

The claim of consensus deviation via eclipse is speculative: `raw_session` still performs a cryptographic handshake, so the attacker's actual `PeerId` is revealed and the target is not truly eclipsed. The concrete, proven impact is resource exhaustion and disruption of the hole-punching path.

## Likelihood Explanation

Preconditions: one standard P2P connection to the target (or any relay node). No special privileges, no leaked keys, no majority hashpower. The `ConnectionRequest` and `ConnectionSync` messages are standard production protocol messages. The spoofed `from` field passes all existing validation because the only structural check is that it decodes as a valid `PeerId` (L36-38). The attack is locally reproducible: connect a test peer with `PeerId A`, send `ConnectionRequest{from=B, to=T, listen_addrs=[attacker_addr]}`, observe `pending_delivered[B] = attacker_addr`, then send `ConnectionSync{from=B, to=T}` and observe NAT traversal to `attacker_addr`.

## Recommendation

In `ConnectionRequestProcess::execute` (or in `received` before dispatch), look up the session's actual `PeerId` from the peer registry using `self.peer` (the `PeerIndex`) via `network_state.with_peer_registry(|reg| reg.get_peer(self.peer))`, extract the `PeerId` from `peer.connected_addr`, and assert it equals `content.from`. Reject (and optionally ban) the session if they differ.

Apply the same fix to `ConnectionSyncProcess::execute` for the `from` field.

For forwarded messages (where `from` is a remote originator, not the immediate sender), enforce the `from == session PeerId` invariant **only for the first hop** (i.e., when `route` is empty), since relay nodes legitimately forward messages on behalf of the original sender. The `route` field already records the forwarding path and can be used to distinguish first-hop from relay cases.

## Proof of Concept

```
1. Attacker node (PeerId=A) establishes a standard P2P connection to target T.

2. Attacker sends ConnectionRequest:
     from        = victim_peer_id (B, any known PeerId)
     to          = T's own PeerId
     listen_addrs = [attacker_ip:attacker_port/p2p/B]
     max_hops    = 1
     route       = []

3. T's ConnectionRequestProcess::execute:
   - content.from = B (wire-supplied, unchecked)
   - self_peer_id == content.to → calls respond_delivered(B, T, [attacker_addr])
   - pending_delivered.insert(B, ([attacker_addr], now))

4. Attacker sends ConnectionSync:
     from  = B
     to    = T
     route = []

5. T's ConnectionSyncProcess::execute:
   - self_peer_id == content.to
   - listens_info = pending_delivered.get(B) = [attacker_addr]
   - spawns try_nat_traversal(bind_addr, attacker_addr) — 30-second TCP retry loop

6. Attacker repeats steps 2-5 with fresh victim PeerIds (C, D, E, …) every 2 minutes
   to bypass HOLE_PUNCHING_INTERVAL, continuously exhausting T's socket/thread resources.

Verification: add a debug log or test assertion after pending_delivered.insert() and
observe it fires with from_peer_id=B while the actual session PeerId is A.
```