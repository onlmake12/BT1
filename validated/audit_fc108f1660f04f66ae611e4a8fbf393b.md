I have read all the relevant source files. Let me verify the key claims against the actual code before rendering a verdict.

Audit Report

## Title
Missing `from == to == local_peer_id` Guard Enables Attacker-Triggered Loopback NAT Traversal and Inbound Slot Exhaustion — (`network/src/protocols/hole_punching/component/connection_request.rs`, `connection_sync.rs`)

## Summary
Neither `ConnectionRequestProcess::execute` nor `ConnectionSyncProcess::execute` validates that `content.from != content.to` or that either field differs from `local_peer_id`. An attacker with one established P2P session can send a crafted `ConnectionRequest` with `from=to=local_peer_id` to poison `pending_delivered[local_peer_id]` with the victim's own listen address, then send one `ConnectionSync` per second to trigger loopback TCP connections that are registered as inbound `raw_session` entries, exhausting the node's inbound connection slots and isolating it from legitimate peers.

## Finding Description

**Root cause:** No guard for `from == to` or `from/to == local_peer_id` exists in either handler.

**Step 1 — Poison `pending_delivered[local_id]`**

Attacker sends `ConnectionRequest{from=local_id, to=local_id, listen_addrs=[victim_listen_addr], route=[], max_hops=1}`.

In `ConnectionRequestProcess::execute` (`connection_request.rs` L110–153):
- L128: `content.route.contains(self_peer_id)` — route is empty, guard does not fire.
- L132–143: `forward_rate_limiter.check_key(&(local_id, local_id, item_id))` — passes on first call.
- L145: `self_peer_id == &content.to` evaluates `true` (both are `local_id`) → `respond_delivered(local_id, &local_id, [victim_listen_addr])` is called.

Inside `respond_delivered` (L155–240):
- L161–167: `pending_delivered.get(&local_id)` is `None` initially; cooldown check skipped.
- L196–215: `victim_listen_addr` is TCP/IP4 or TCP/IP6 — passes the transport filter.
- L226–232: `ConnectionDelivered` is sent back to the attacker's session (succeeds).
- L234–237: `pending_delivered.insert(local_id, ([victim_listen_addr], now))` — entry poisoned.

**Step 2 — Trigger loopback NAT traversal**

Attacker sends `ConnectionSync{from=local_id, to=local_id, route=[]}` once per second.

In `ConnectionSyncProcess::execute` (`connection_sync.rs` L76–176):
- L82–84: empty route length check passes.
- L85–96: `forward_rate_limiter.check_key(&(local_id, local_id, item_id))` — 1 req/sec quota (`mod.rs` L256), passes once per second.
- L98: `content.route.last()` is `None` → `None` branch taken.
- L102: `self_peer_id != &content.to` is `false` (both `local_id`) → else branch executes.
- L111–115: `pending_delivered.get(&local_id)` returns the poisoned `[victim_listen_addr]`.
- L119–124: `try_nat_traversal` tasks are created for each address.
- L145–162: `runtime::spawn` fires a task; on success calls `control.raw_session(stream, addr, RawSessionInfo::inbound(listen_addr))`.

**Why `try_nat_traversal` succeeds immediately:** The victim is actively listening on `victim_listen_addr`. A TCP connection from the victim to its own listen port succeeds on the first attempt (loopback), so the retry loop exits immediately and returns the stream.

**Existing guards are insufficient:**
- The per-session `rate_limiter` (30 req/sec, `mod.rs` L251) limits message volume per session but does not block the semantic abuse.
- The `forward_rate_limiter` (1 req/sec per `(from, to, item_id)` key, `mod.rs` L256) limits to 1 trigger/second, but with `from == to == local_id` the key is fixed and the attacker can sustain exactly 1 loopback connection per second indefinitely.
- The `HOLE_PUNCHING_INTERVAL` (2-minute) cooldown in `respond_delivered` (L161–167) only throttles re-poisoning; the existing entry survives for `TIMEOUT` = 5 minutes (`mod.rs` L28) and can be refreshed every 2 minutes.
- No guard anywhere checks `from == to` or either field equalling `local_peer_id`.

## Impact Explanation

Each successful `raw_session` call registers a loopback TCP stream as an inbound connection, consuming one inbound slot. At 1 slot/second, the attacker exhausts the node's inbound connection pool within seconds to minutes (depending on the configured limit), preventing any legitimate peer from establishing a new inbound connection. This constitutes a targeted crash/isolation of a CKB node's networking layer, matching **High: Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

The attack requires only one established P2P session — no special privileges, no cryptographic material, no proof-of-work. The victim's `local_peer_id` and listen address are both public (exchanged during the P2P handshake and advertised in the network). The two-message sequence is trivially crafted by setting `from` and `to` bytes in the molecule-encoded message to the victim's own peer ID bytes. The attack is repeatable and sustainable as long as the session remains open.

## Recommendation

Add an early-rejection guard at the top of both `ConnectionRequestProcess::execute` and `ConnectionSyncProcess::execute`, before any rate-limiter keying or `pending_delivered` lookup:

```rust
if content.from == content.to
    || &content.from == self_peer_id
    || &content.to == self_peer_id
{
    return StatusCode::Ignore.with_context("invalid from/to peer id");
}
```

This must be checked before the `forward_rate_limiter` call to prevent the fixed-key rate-limiter slot from being consumed by the attacker.

## Proof of Concept

```
1. Attacker connects to victim, establishing session S.
   (Attacker learns victim's local_peer_id and listen_addr from the handshake/peer store.)

2. Attacker sends over S:
     ConnectionRequest {
       from         = victim_local_peer_id,
       to           = victim_local_peer_id,
       listen_addrs = [/ip4/<victim_ip>/tcp/<victim_port>],
       route        = [],
       max_hops     = 1,
     }
   Expected: victim calls respond_delivered → inserts
     pending_delivered[local_id] = ([/ip4/<victim_ip>/tcp/<victim_port>], now)

3. Attacker sends over S once per second:
     ConnectionSync {
       from  = victim_local_peer_id,
       to    = victim_local_peer_id,
       route = [],
     }
   Expected per message:
     - pending_delivered lookup hits the poisoned entry
     - try_nat_traversal connects to victim's own listen port (succeeds immediately)
     - control.raw_session(loopback_stream, addr, RawSessionInfo::inbound(...)) called
     - victim's inbound connection count increments by 1

4. Assert after N seconds:
     - victim's inbound slot count == N
     - legitimate peers are refused new inbound connections
     - peer registry contains self-referential entries
```