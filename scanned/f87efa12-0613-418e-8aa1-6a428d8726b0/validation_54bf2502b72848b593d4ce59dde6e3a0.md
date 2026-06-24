Looking at the actual code to verify the claim.

The code is confirmed. All cited line numbers and behavior match exactly. Let me render the final verdict.

---

Audit Report

## Title
Hole-Punching Cooldown Bypass via Failed Async Send Leaving `pending_delivered` Unpopulated — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary
In `respond_delivered`, the `pending_delivered` map is updated only after a successful `send_message_to` call. If the send fails (e.g., the requesting peer closes the TCP connection immediately), the function returns early at line 231 and the cooldown entry is never written. Because the cooldown check at line 161 finds no entry on the next connection, the 2-minute `HOLE_PUNCHING_INTERVAL` is never enforced for that originator, allowing repeated triggering of the response-building path at the cost of TCP reconnections.

## Finding Description
`respond_delivered` (lines 155–240) is invoked when the local node is the `to` target of a `ConnectionRequest`. The cooldown guard reads `pending_delivered` at line 161; if no entry exists, execution continues unconditionally. The function then collects up to 24 public/observed addresses, builds a `ConnectionRequestDelivered` message, and calls `send_message_to` at line 226. If that call returns `Err`, line 231 returns `ForwardError` and lines 234–237 are never reached — `pending_delivered` is not updated.

On the next connection the attacker opens, the session-keyed `rate_limiter` (keyed by `(PeerIndex, u32)`) is fresh because the session ID changed. The `forward_rate_limiter` (keyed by `(PeerId, PeerId, u32)`) can be bypassed by incrementing `item_id`. The `pending_delivered` check again finds no entry and proceeds. This cycle repeats indefinitely with no cooldown ever taking effect.

## Impact Explanation
Each bypass forces the victim node to collect addresses, serialize a response, and attempt an async send — all wasted work. The `pending_delivered` map is also the source of remote listen addresses used for subsequent NAT traversal attempts; leaving it unpopulated for a targeted originator permanently degrades hole-punching for that peer identity. The sustained cost maps to **Low (501–2000 points): any other important performance improvements for CKB**, as the per-iteration work (address collection, message serialization, failed async I/O) is real but lightweight, and the attack rate is bounded by TCP reconnection overhead rather than constituting node-crashing or network-wide congestion.

## Likelihood Explanation
Any unprivileged peer reachable over the P2P network can trigger this. The only requirement is the ability to open TCP connections and close them at will before the victim's `send_message_to` completes. No keys, privileges, or hash-power are needed. The attack is directly reachable from the `received` handler exposed to all connected peers.

## Recommendation
Move the `pending_delivered.insert` call to before `send_message_to`, following the checks-effects-interactions pattern:

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
    return StatusCode::ForwardError.with_context(error);
}
```

This ensures the cooldown is enforced regardless of whether the send succeeds or fails.

## Proof of Concept
1. Attacker peer connects to victim node (new session ID, fresh rate limiter slot).
2. Attacker sends a valid `ConnectionRequest` with `to = victim_peer_id` and `item_id = N`.
3. Victim's `respond_delivered` passes the `pending_delivered` check (no entry), collects addresses, builds response, and calls `send_message_to(attacker_session, ...)`.
4. Attacker closes the TCP connection immediately → `send_message_to` returns `Err`.
5. `respond_delivered` returns `ForwardError`; `pending_delivered` is not updated.
6. Attacker reconnects (new session ID, rate limiter reset), sends `ConnectionRequest` with `item_id = N+1` (bypasses `forward_rate_limiter`).
7. `pending_delivered` check passes again → repeat from step 3.