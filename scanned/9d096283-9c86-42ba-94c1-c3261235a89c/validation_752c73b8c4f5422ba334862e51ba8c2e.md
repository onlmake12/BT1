Audit Report

## Title
Unauthenticated `ConnectionRequestDelivered` Relay Forwarding with Rate-Limiter Bypass Enables Relay Abuse and Conditional NAT-State Poisoning — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

## Summary
`ConnectionRequestDeliveredProcess::execute` forwards a `ConnectionRequestDelivered` message to the peer identified by `content.from` when `route` is empty and the relay is not `content.from`, with no verification that the sending session corresponds to `content.to` or that a prior `ConnectionRequest` for the `(from, to)` pair was ever relayed. The forward rate limiter is keyed on `(content.from, content.to, msg_item_id)`, so an attacker can bypass it by varying `content.to`. Combined, these flaws allow an unprivileged peer to abuse any relay as a message forwarder and, conditionally, to trigger unbounded `try_nat_traversal` tasks on victim nodes.

## Finding Description
**Relay forwarding without sender verification (L147–153):**
```rust
match content.route.last() {
    Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
    None => {
        let self_peer_id = self.protocol.network_state.local_peer_id();
        if self_peer_id != &content.from {
            self.forward_delivered(&content.from).await  // no check that self.peer == content.to
```
The only guard is `self_peer_id != content.from`. There is no check that `self.peer` (the session that sent this message) is the peer identified by `content.to`, and no relay-side record that a `ConnectionRequest` for `(from, to)` was ever forwarded. Any connected peer can set `from=victim_peer_id`, `route=[]`, and the relay will look up the victim in `peer_registry` and deliver the message verbatim.

**`forward_delivered` preserves all attacker-supplied fields (L182–213, mod.rs L228–240):**
`forward_delivered(self.message)` copies the original message with only the route shortened. `listen_addrs`, `to`, and `sync_route` are passed through unchanged. When `route` is already empty, the route field is rebuilt as an empty `BytesVec`, so the forwarded message arrives at the victim with `route.last() == None`.

**Rate-limiter bypass (L134–145):**
```rust
.check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
```
The key includes `content.to`, which the attacker fully controls. By cycling through distinct `content.to` values (all syntactically valid `PeerId` bytes), the attacker creates a fresh rate-limiter bucket per message, bypassing the per-`(from, to)` limit entirely. The only effective cap is the outer per-session limit.

**NAT traversal with attacker-controlled addresses (L160–176):**
When the victim receives the forwarded message, it executes the terminal branch: `inflight_requests.remove(&content.to)`. If the victim has an active inflight request for `content.to` (observable via gossip), it calls `self.try_nat_traversal(ttl, content.listen_addrs)`. `try_nat_traversal` (mod.rs L49–115) spawns a task that loops for up to 30 seconds, making TCP `connect` calls every ~200 ms to the attacker-supplied address. Each triggered traversal spawns a new `runtime::spawn` task, consuming file descriptors and CPU for the full 30-second window.

## Impact Explanation
**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs / crash a CKB node.**

- **Relay amplification / network congestion:** With the rate-limiter bypass, an attacker with a single session can send up to the per-session cap of messages per second, each causing the relay to perform a registry lookup and send a message to a different victim. With multiple sessions (trivially obtained), the throughput scales linearly. Relays become involuntary forwarders, generating traffic proportional to the attacker's connection count.
- **Node resource exhaustion:** Each successful NAT-traversal trigger spawns a 30-second task making ~150 TCP `connect` calls. An attacker who can observe multiple active `inflight_requests` entries (via gossip) can trigger multiple concurrent traversal tasks on a victim node, exhausting file descriptors and CPU, potentially crashing the node.

## Likelihood Explanation
- A standard P2P connection to any relay is sufficient — no special privileges required.
- `PeerId` values are public (advertised via the identify protocol and peer store).
- The relay does not require any prior `ConnectionRequest` for the `(from, to)` pair.
- The NAT-poisoning branch additionally requires the attacker to know an active `inflight_requests` entry on the victim; `ConnectionRequest` messages are gossiped, making `(from, to)` pairs observable.
- The rate-limiter bypass requires only varying `content.to` across messages, which is trivial.
- The attack is repeatable and requires no victim interaction.

## Recommendation
1. **Verify sender identity at the relay:** Before forwarding, assert that `self.peer` corresponds to the peer identified by `content.to`. Reject the message if the sending session does not match `content.to`.
2. **Track forwarded requests:** Maintain a relay-side set of `(from, to)` pairs for which a `ConnectionRequest` was forwarded, and only forward `ConnectionRequestDelivered` for known pairs. Expire entries after a reasonable TTL.
3. **Bind the rate limiter to the sending session:** Key the forward rate limiter on `(session_id, from, to)` or `(session_id, msg_item_id)` rather than `(from, to, msg_item_id)` to prevent bypass via `to` variation.

## Proof of Concept
```
1. Attacker A establishes a standard P2P connection to relay R.
2. Victim V is connected to R; A learns V's PeerId via identify/peer-store gossip.
3. A crafts:
     ConnectionRequestDelivered {
       from: V.peer_id,
       to:   <any syntactically valid PeerId, varied per message>,
       route: [],
       sync_route: [],
       listen_addrs: [attacker_controlled_ip:port/p2p/<to_peer_id>],
     }
4. A sends this to R over the HolePunching protocol.
5. R: route.last() == None, self_peer_id != V.peer_id → forward_delivered(V.peer_id).
6. R looks up V's session in peer_registry and sends the message to V.
   (Rate limiter is bypassed because content.to differs each time.)
7. V receives it: route.last() == None, self_peer_id == content.from → terminal branch.
8. If V has inflight_requests[content.to], V calls try_nat_traversal(ttl, [attacker_addr]).
9. V spawns a 30-second task making TCP SYN packets to attacker_controlled_addr.
10. Repeat step 3–9 with different content.to values to trigger multiple concurrent
    traversal tasks, exhausting V's file descriptors.

Unit test plan: construct a mock HolePunching protocol with a peer_registry containing
a victim session; call execute() with a crafted message from a non-to session; assert
forward_delivered is called and no sender-identity check fires.
```