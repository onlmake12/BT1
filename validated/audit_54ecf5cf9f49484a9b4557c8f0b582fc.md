Audit Report

## Title
Unauthenticated `from` Field in `ConnectionRequest` Enables Targeted DoS of Hole-Punching Protocol — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary

The `ConnectionRequest` handler parses the `from` peer ID entirely from the attacker-controlled message payload and never compares it against the authenticated session peer ID. Any connected peer can therefore set `from` to an arbitrary victim peer ID. The concrete exploitable consequence is that an attacker can pre-poison the `pending_delivered` map on any target node for a victim's peer ID, silently blocking all legitimate hole-punching requests from that victim for the 2-minute `HOLE_PUNCHING_INTERVAL` window — repeatable indefinitely at negligible cost.

## Finding Description

**Root cause.** In `connection_request.rs` L36–38, `from` is deserialized from raw message bytes:

```rust
let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
    StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
})?;
```

The only validation is syntactic (valid `PeerId` bytes) and structural (any peer ID embedded in `listen_addrs` must match `from`). The session's authenticated peer ID is available — `context.session.id` is passed as the `peer: PeerIndex` field of `ConnectionRequestProcess` in `mod.rs` L114 — but it is never compared to `content.from` anywhere in `execute()` or its callees.

**DoS exploit path.**

1. Attacker (peer A, connected to target node T) sends a `ConnectionRequest` with `from = victim_peer_id` (peer B's ID), `to = T's peer ID`, and `listen_addrs` containing attacker-controlled TCP addresses (with peer B's ID appended to satisfy the addr-consistency check at L47–54).
2. `execute()` (L110–153) passes all validation: `from` is a valid `PeerId` ✓, addr peer IDs match `from` ✓, route/hop checks pass ✓.
3. Because `self_peer_id == content.to`, `respond_delivered(content.from, ...)` is called (L146).
4. `respond_delivered` (L161–166) checks `pending_delivered.get(&from_peer_id)` — initially empty — so it proceeds, sends a `ConnectionRequestDelivered` back to the attacker's actual session (L226–229), and inserts `(attacker_addrs, now)` into `pending_delivered[victim_peer_id]` (L235–237).
5. For the next `HOLE_PUNCHING_INTERVAL` (2 minutes), any legitimate `ConnectionRequest` from the real peer B with `from = victim_peer_id` hits the guard at L161–166 and returns `StatusCode::Ignore`.
6. The attacker re-sends one message every 2 minutes to maintain the block indefinitely.

**Why existing mitigations are insufficient.**

- The per-session `rate_limiter` (keyed by `(session_id, msg_item_id)`, 30 req/s) limits throughput but does not prevent the single poisoning message needed.
- The `forward_rate_limiter` (keyed by `(content.from, content.to, msg_item_id)`, 1/s) rate-limits by the spoofed `from`, not the real session, so it provides no protection against spoofing.
- The addr-consistency check (L47–54) only verifies internal message consistency, not that `from` matches the session.

**Secondary effect — wasted NAT traversal resources.** When a subsequent `ConnectionSync` arrives with `from = victim_peer_id`, the target node looks up `pending_delivered[victim_peer_id]` (connection_sync.rs L113–115), retrieves the attacker's addresses, and spawns `try_nat_traversal` tasks (L119–163) that retry TCP connections to the attacker's infrastructure for up to 30 seconds each. The attacker can accept these connections, but cannot authenticate as the victim (TLS/Noise requires the victim's private key), so no session is established — but CPU and socket resources are consumed on the target.

**Note on the "connection hijacking" claim.** The submitted report's claim of MitM/connection hijacking is not achievable: `raw_session` initiates the normal TLS/Noise handshake after the TCP connection, and the attacker cannot present the victim's key material. The actual impact is limited to the DoS and resource-waste paths described above.

## Impact Explanation

The concrete impact is a targeted, low-cost, repeatable denial-of-service against the hole-punching NAT traversal protocol for specific victim peer IDs. An attacker can permanently prevent any chosen peer from successfully completing hole-punching through any node the attacker is connected to, for as long as the attacker maintains the connection and sends one spoofed message every 2 minutes. This maps to **Low (501–2000 points): Any other important performance/connectivity improvement for CKB**, as it degrades NAT traversal availability for targeted peers without crashing nodes or affecting consensus.

## Likelihood Explanation

The attacker requires only a standard peer connection to the target node — no privileged access, no key material, no special network position. The spoofed message passes all existing validation because checks are purely structural. The attack is trivially scriptable, costs one message per 2-minute window per victim, and is repeatable indefinitely. Any unprivileged network peer can execute it.

## Recommendation

After parsing `content.from`, verify it against the authenticated session peer ID before proceeding:

```rust
// In execute(), after parsing content:
let actual_peer_id = self.protocol.network_state
    .with_peer_registry(|reg| {
        reg.get_peer(self.peer).map(|p| extract_peer_id(&p.connected_addr))
    })
    .flatten();

if actual_peer_id.as_ref() != Some(&content.from) {
    return StatusCode::InvalidFromPeerId
        .with_context("from field does not match authenticated session peer id");
}
```

This ensures `from` is cryptographically bound to the session, consistent with how other protocols (e.g., `identify/mod.rs`) use `context.session.id` to look up peer state rather than trusting peer-supplied identity fields.

## Proof of Concept

1. Connect peer A to a CKB node T (which is also the intended `to` target).
2. Peer A sends a molecule-encoded `HolePunchingMessage::ConnectionRequest` on the `HolePunching` protocol with:
   - `from` = known peer B's `PeerId` bytes
   - `to` = node T's `PeerId` bytes
   - `listen_addrs` = one attacker-controlled TCP address with peer B's peer ID appended as `/p2p/<B>`
   - `max_hops` = 1, `route` = []
3. Node T processes the message: `from` is valid ✓, addr peer ID matches `from` ✓, `self_peer_id == to` → calls `respond_delivered(victim_peer_id, ...)`.
4. Node T inserts `(attacker_addr, now)` into `pending_delivered[victim_peer_id]`.
5. Send a legitimate `ConnectionRequest` from the real peer B with `from = victim_peer_id` to node T within 2 minutes → node T returns `StatusCode::Ignore` (rate-limited by the poisoned entry).
6. Repeat step 2 every 2 minutes to maintain the block indefinitely.
7. Optionally: send a `ConnectionSync` with `from = victim_peer_id` to trigger `try_nat_traversal` toward the attacker's address, consuming target resources for 30 seconds.