Audit Report

## Title
Unauthenticated `from` Field in `ConnectionRequestDelivered` Enables Inflight-Request Cancellation and Forced NAT Traversal to Attacker-Controlled Addresses — (File: `network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

## Summary
The `ConnectionRequestDelivered` handler determines whether the local node is the originator of a hole-punching request by comparing its own peer ID against the wire-supplied `content.from` field. Because `content.from` is never validated against the authenticated sender's peer ID, any connected peer can spoof `from = local_peer_id` with an empty `route`, enter the originator branch, cancel legitimate inflight hole-punching requests, and force the node to make repeated outbound TCP connections to attacker-controlled addresses for up to 30 seconds per address.

## Finding Description
In `DeliverdContent::try_from` (lines 38–40), `content.from` is parsed from the wire message using only syntactic validation (`PeerId::from_bytes`); it is never cross-checked against the actual sender's authenticated `PeerIndex` (`self.peer`):

```rust
let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
    StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
})?;
```

In `execute()` (lines 147–179), when `content.route` is empty, the code compares the local peer ID against `content.from`:

```rust
let self_peer_id = self.protocol.network_state.local_peer_id();
if self_peer_id != &content.from {
    self.forward_delivered(&content.from).await
} else {
    // originator branch
    let request_start = self.protocol.inflight_requests.remove(&content.to);
    match request_start {
        Some(start) => {
            let res = self.respond_sync(content.from).await;
            ...
            self.try_nat_traversal(ttl, content.listen_addrs);
            Status::ok()
        }
        None => StatusCode::Ignore.with_context("the request is not in flight"),
    }
}
```

An attacker who sets `from = local_peer_id` (public, broadcast via Identify) and `route = []` passes this check and enters the originator branch. The `forward_rate_limiter` keys on `(content.from, content.to, msg_item_id)` where `msg_item_id` is the fixed message-type ID — the attacker can vary `content.to` across different inflight entries to bypass per-key rate limiting. The session-level rate limiter allows 30 messages/second from a single peer.

The `try_nat_traversal` function (component/mod.rs lines 49–115) makes repeated TCP connection attempts to each supplied address for up to 30 seconds (200ms timeout per attempt, ~150 attempts per address), with up to 24 addresses concurrently. Inflight entries are observable from gossiped `ConnectionRequest` broadcasts (sent to sqrt(N) peers every 5 minutes).

## Impact Explanation
**Inflight request cancellation (DoS on hole-punching):** `inflight_requests.remove(&content.to)` permanently removes the entry. An attacker who observes a gossiped `ConnectionRequest` to peer `T` can cancel the victim's inflight hole-punching attempt to `T` by sending a single spoofed `ConnectionRequestDelivered`. The victim silently abandons the attempt. Inflight entries are repopulated only every 5 minutes (`CHECK_INTERVAL`), so repeated cancellation persistently prevents the victim from completing hole-punching and establishing new outbound connections — matching **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"** (High, 10001–15000 points), as a node unable to punch through NAT cannot expand its peer set, contributing to network connectivity degradation.

**Forced outbound TCP connections to attacker-controlled addresses:** Each triggered `try_nat_traversal` spawns an async task making ~150 TCP connection attempts per address over 30 seconds, across up to 24 addresses concurrently. This enables port scanning of internal networks reachable from the victim and resource exhaustion (socket handles, async task memory) proportional to the number of inflight entries the attacker can cancel.

## Likelihood Explanation
- **Entry path:** Any peer connected to the victim over the P2P network can send `HolePunchingMessage::ConnectionRequestDelivered`. No special role or key is required.
- **Required knowledge:** The local peer ID is public (Identify protocol). Inflight `to` peer IDs are observable from gossiped `ConnectionRequest` messages broadcast to sqrt(N) peers.
- **Rate limiting:** The session-level limiter (30/sec) and `forward_rate_limiter` (1/sec per `(from, to, item_id)`) do not prevent the attack: the attacker needs only one message per inflight entry to cancel it, and inflight entries are a small set (bounded by `max_outbound - non_whitelist_outbound`).
- **Repeatability:** The attack repeats every 5-minute `notify` cycle as new inflight entries are populated.

## Recommendation
Before entering the originator branch, resolve the actual sender's `PeerId` from the authenticated session via `self.peer` (a `PeerIndex`) and compare it against `content.from`. The peer registry already supports this lookup (`get_key_by_peer_id` / `get_peer`). If `content.from` does not match the authenticated sender's peer ID, reject the message or treat it as a forwarding case rather than a terminal delivery. This ensures the originator branch is only reachable by the genuine originator.

## Proof of Concept
**Setup:** Attacker peer `A` is connected to victim `V`. `V` has recently broadcast a `ConnectionRequest` to peer `T` (observed by `A`), so `V.inflight_requests[T] = timestamp`.

**Step 1:** `A` crafts a `ConnectionRequestDelivered` molecule message:
- `from` = `V`'s peer ID (obtained from Identify handshake)
- `to` = `T`'s peer ID (observed from `V`'s gossiped `ConnectionRequest`)
- `route` = `[]`
- `sync_route` = `[]`
- `listen_addrs` = `[/ip4/192.168.1.1/tcp/9999/p2p/<T_peer_id>]` (attacker-controlled)

**Step 2:** `A` sends this message to `V` over the HolePunching protocol channel.

**Step 3:** `V` processes the message:
- `content.route.last()` → `None`
- `self_peer_id == &content.from` → **true** (spoofed)
- `inflight_requests.remove(&T)` → `Some(timestamp)` — **inflight request permanently cancelled**
- `try_nat_traversal(ttl, [/ip4/192.168.1.1/tcp/9999/...])` → **V makes ~150 TCP connection attempts to 192.168.1.1:9999 over 30 seconds**

**Verification:** A unit test can mock `network_state.local_peer_id()` and `inflight_requests`, send a crafted message with `from = local_peer_id` and `route = []`, and assert that (a) `inflight_requests` no longer contains the `to` entry and (b) `try_nat_traversal` is invoked with the attacker-supplied addresses.