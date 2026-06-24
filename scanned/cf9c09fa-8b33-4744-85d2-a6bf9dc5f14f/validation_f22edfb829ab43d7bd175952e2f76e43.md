The code has been confirmed. Let me verify the key claims against the actual source.

Audit Report

## Title
Hole-Punching Cooldown Bypass via Post-Send State Update on Failed Async Send — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary
In `respond_delivered`, the `pending_delivered` cooldown entry is inserted only after a successful `send_message_to` call. If the send fails (e.g., the requesting peer closes the TCP connection immediately after sending the `ConnectionRequest`), the function returns early via `StatusCode::ForwardError` and `pending_delivered` is never updated. Because the cooldown check at the top of `respond_delivered` finds no entry, the 2-minute `HOLE_PUNCHING_INTERVAL` is never enforced, allowing any unprivileged peer to repeatedly trigger the full response-building path at the cost of TCP reconnects alone.

## Finding Description
The control flow in `respond_delivered` (lines 155–240 of `connection_request.rs`) is:

1. **Cooldown check** (L161–167): reads `pending_delivered` for `from_peer_id`; if no entry exists, proceeds unconditionally.
2. **Response construction** (L168–224): collects up to 24 public/observed addresses, builds a `ConnectionRequestDelivered` packed message.
3. **Async send** (L226–232): calls `send_message_to(self.peer, proto_id, new_message).await`; on error, returns `ForwardError` immediately.
4. **State update** (L234–237): inserts `(remote_listens, now)` into `pending_delivered` — only reached on success.

If the attacker closes the TCP connection after sending the `ConnectionRequest`, step 3 returns `Err`, step 4 is never reached, and `pending_delivered` remains empty for `from_peer_id`. On the next reconnect, step 1 finds no entry and the full path executes again.

The two rate limiters do not compensate:
- `rate_limiter` is keyed by `(PeerIndex, u32)` — it is session-scoped and resets on every reconnect (L67–69 of `mod.rs` calls `retain_recent` on disconnect, not a hard reset, but a new `PeerIndex` is assigned on each new session).
- `forward_rate_limiter` is keyed by `(PeerId, PeerId, u32)` at 1 req/s per unique `(from, to, item_id)` tuple. Incrementing `item_id` on each reconnect produces a fresh key, bypassing this limiter entirely.

`HOLE_PUNCHING_INTERVAL` is 2 minutes (L24 of `mod.rs`), meaning the intended protection window is completely unenforced.

## Impact Explanation
Each bypass iteration forces the victim node to parse the `ConnectionRequest`, query and serialize up to 24 public/observed addresses, build a packed `ConnectionRequestDelivered` message, and perform an async send that fails. Because no cooldown is ever recorded, this can be repeated at the rate of TCP reconnections with no upper bound imposed by the protocol. This constitutes a low-cost, high-repetition resource exhaustion attack against a targeted CKB node, matching the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs** (10001–15000 points). A single attacker with modest bandwidth can sustain the attack indefinitely; multiple coordinated attackers amplify it further.

## Likelihood Explanation
Any peer reachable over the P2P network can trigger this. No special privileges, cryptographic keys, or stake are required. The only cost per iteration is one TCP handshake and one protocol message. The attack is directly reachable from the `received` handler, which is exposed to all connected peers. The `forward_rate_limiter` bypass via `item_id` increment is trivial to implement in any custom P2P client.

## Recommendation
Apply the checks-effects-interactions pattern: insert into `pending_delivered` **before** calling `send_message_to`. This ensures the cooldown is enforced regardless of whether the send succeeds or fails:

```rust
// Insert BEFORE the external call
let now = unix_time_as_millis();
self.protocol
    .pending_delivered
    .insert(from_peer_id.clone(), (remote_listens, now));

if let Err(error) = self
    .p2p_control
    .send_message_to(self.peer, proto_id, new_message)
    .await
{
    // Cooldown is already recorded; early return is safe
    return StatusCode::ForwardError.with_context(error);
}
```

Optionally, on send failure the entry can be removed if the design intent is to only record successful deliveries, but the cooldown should still be enforced to prevent the bypass.

## Proof of Concept
1. Attacker peer connects to victim node over TCP.
2. Attacker sends a `ConnectionRequest` with `to = victim_peer_id`, `item_id = N`, and valid `listen_addrs`.
3. Attacker immediately closes the TCP connection.
4. Victim's `respond_delivered` passes the `pending_delivered` check (no entry), builds the `ConnectionRequestDelivered` response, calls `send_message_to` — which returns `Err` because the session is gone.
5. `respond_delivered` returns `ForwardError`; `pending_delivered` is **not** updated.
6. Attacker reconnects (new session, new `PeerIndex`), sends `ConnectionRequest` with `item_id = N+1` (new key in `forward_rate_limiter`).
7. `pending_delivered` check passes again — repeat from step 3.

Each iteration forces the victim to rebuild and attempt to send a `ConnectionRequestDelivered` message. The 2-minute cooldown never takes effect. A script that loops TCP connect → send → disconnect at ~10 iterations/second sustains continuous resource consumption on the victim with no protocol-level enforcement stopping it.