### Title
Hole-Punching Cooldown Bypass via State Update After Failed Async Send — (`network/src/protocols/hole_punching/component/connection_request.rs`)

### Summary

In `respond_delivered`, the `pending_delivered` cooldown timestamp is written **after** the async `send_message_to` call. If the send returns an error (e.g., the requesting peer closes the connection), the function returns early and `pending_delivered` is never updated. This leaves the cooldown unenforced, allowing any unprivileged peer to bypass the `HOLE_PUNCHING_INTERVAL` (2-minute) rate-limit on hole-punching responses.

### Finding Description

`respond_delivered` in `ConnectionRequestProcess` is the handler invoked when the local node is the target of a `ConnectionRequest` hole-punching message. It enforces a per-originator cooldown via `pending_delivered`:

```
// 1. CHECK – read cooldown state
if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
    if now - t < HOLE_PUNCHING_INTERVAL { return Ignore; }
}

// 2. EXTERNAL CALL – async send to requesting peer
if let Err(error) = self.p2p_control
    .send_message_to(self.peer, proto_id, new_message).await
{
    return StatusCode::ForwardError.with_context(error);  // ← early return
}

// 3. STATE UPDATE – only reached on success
self.protocol.pending_delivered.insert(from_peer_id, (remote_listens, now));
``` [1](#0-0) [2](#0-1) 

The `HOLE_PUNCHING_INTERVAL` constant is 2 minutes: [3](#0-2) 

`pending_delivered` is declared as a plain `HashMap<PeerId, PendingDeliveredInfo>` on the `HolePunching` struct: [4](#0-3) 

Because the state update is gated on a successful send, a peer that causes the send to fail (by closing the TCP connection immediately after forwarding the `ConnectionRequest`) leaves `pending_delivered` unmodified. On the next connection the cooldown check at line 161 finds no entry and proceeds unconditionally.

The per-session `rate_limiter` (30 req/s) is reset on every new session, and the `forward_rate_limiter` is keyed by `(from, to, item_id)`: [5](#0-4) 

A peer that increments `item_id` on each reconnect bypasses both limiters.

### Impact Explanation

Each successful bypass forces the target node to:
1. Parse and validate the `ConnectionRequest` message.
2. Build a `ConnectionRequestDelivered` response (including collecting public/observed addresses).
3. Attempt an async send that fails.

Because `pending_delivered` is never populated, the 2-minute cooldown is never enforced. An attacker can sustain this at the rate of TCP reconnections, causing unbounded CPU and network resource consumption on the victim node. Additionally, the `pending_delivered` map — which also stores remote listen addresses used for NAT traversal — is never populated, permanently degrading hole-punching functionality for the targeted originator identity.

### Likelihood Explanation

Any unprivileged peer reachable over the P2P network can trigger this. The only cost is repeated TCP reconnections. No special privileges, keys, or majority hash-power are required. The attack is directly reachable from the `received` handler, which is exposed to all connected peers.

### Recommendation

Update `pending_delivered` **before** calling `send_message_to`, following the checks-effects-interactions pattern:

```rust
// Update state BEFORE the external call
let now = unix_time_as_millis();
self.protocol
    .pending_delivered
    .insert(from_peer_id.clone(), (remote_listens, now));

// Then perform the external call
if let Err(error) = self
    .p2p_control
    .send_message_to(self.peer, proto_id, new_message)
    .await
{
    // Optionally remove the entry on failure, or leave it to enforce the cooldown
    return StatusCode::ForwardError.with_context(error);
}
```

This mirrors the fix recommended in the reference report: update the guard state before any external call so that even a failed or aborted operation leaves the cooldown enforced.

### Proof of Concept

1. Attacker peer connects to victim node.
2. Attacker sends a `ConnectionRequest` with `to = victim_peer_id`, `item_id = N`.
3. Victim's `respond_delivered` passes the `pending_delivered` check (no entry), builds the response, and calls `send_message_to(attacker_session, ...)`.
4. Attacker closes the TCP connection immediately → `send_message_to` returns `Err`.
5. `respond_delivered` returns `ForwardError`; `pending_delivered` is **not** updated.
6. Attacker reconnects (new session, rate limiter reset), sends `ConnectionRequest` with `item_id = N+1` (bypasses `forward_rate_limiter`).
7. `pending_delivered` check passes again → repeat from step 3.

Each iteration forces the victim to rebuild and attempt to send a `ConnectionRequestDelivered` message at the cost of a TCP reconnect, with no cooldown ever taking effect.

### Citations

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L161-167)
```rust
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
            }
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L226-237)
```rust
        if let Err(error) = self
            .p2p_control
            .send_message_to(self.peer, proto_id, new_message)
            .await
        {
            return StatusCode::ForwardError.with_context(error);
        }

        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/mod.rs (L24-24)
```rust
pub(crate) const HOLE_PUNCHING_INTERVAL: u64 = 2 * 60 * 1000; // 2 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L38-47)
```rust
pub(crate) struct HolePunching {
    network_state: Arc<NetworkState>,
    bind_addr: Option<SocketAddr>,
    // Request timestamp recorded
    inflight_requests: HashMap<PeerId, u64>,
    // Delivered timestamp recorded
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
}
```
