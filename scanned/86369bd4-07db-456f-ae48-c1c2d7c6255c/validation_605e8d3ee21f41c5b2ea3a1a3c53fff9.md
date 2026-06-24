Audit Report

## Title
Unauthenticated `from` Field in `ConnectionRequest`/`ConnectionSync` Enables Attacker-Controlled NAT Traversal — (`network/src/protocols/hole_punching/component/connection_request.rs`, `connection_sync.rs`)

## Summary
The hole-punching protocol's `ConnectionRequestProcess::respond_delivered` and `ConnectionSyncProcess::execute` never verify that the `from` field in received messages matches the actual sending peer's identity. An attacker with a single legitimate P2P connection can spoof `from=arbitrary_peer_id` and `listen_addrs=[attacker_ip]`, causing the victim to insert the attacker's IP into `pending_delivered[arbitrary_peer_id]`. A subsequent spoofed `ConnectionSync` then causes the victim to spawn a NAT traversal task that calls `raw_session` to the attacker-controlled IP, establishing a new P2P session to an attacker-controlled node. Repeating with fresh spoofed peer IDs fills the victim's connection table with attacker-controlled peers, constituting a targeted eclipse attack.

## Finding Description

**Root cause:** The `from` field in `ConnectionRequest` and `ConnectionSync` wire messages is accepted verbatim with no cryptographic binding to the actual sending session.

**Step 1 — Spoofed `ConnectionRequest`:**

In `ConnectionRequestProcess::execute`, when `self_peer_id == &content.to`, the code calls `respond_delivered(content.from, ...)` directly:

```rust
// connection_request.rs L145-147
if self_peer_id == &content.to {
    self.respond_delivered(content.from, &content.to, content.listen_addrs)
        .await
```

There is no check that `content.from` matches the actual sending peer (`self.peer`). Inside `respond_delivered`, the only guard is a 2-minute cooldown keyed by `from_peer_id` (the spoofed value):

```rust
// connection_request.rs L161-166
if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
    let now = unix_time_as_millis();
    if now - t < HOLE_PUNCHING_INTERVAL {
        return StatusCode::Ignore ...
    }
}
```

This is trivially bypassed by using a different spoofed `from` value each time. After sending `ConnectionRequestDelivered` back to `self.peer` (the attacker's actual session), the attacker's IP is unconditionally inserted:

```rust
// connection_request.rs L234-237
let now = unix_time_as_millis();
self.protocol
    .pending_delivered
    .insert(from_peer_id, (remote_listens, now));
```

The `listen_addrs` validation only checks that any embedded peer ID in the address matches `content.from` (the spoofed value), not the actual sender — so the attacker simply includes their IP with the spoofed peer ID embedded.

**Step 2 — Spoofed `ConnectionSync`:**

`ConnectionSyncProcess` has no `peer` field at all — it cannot identify the actual sender. When `content.route` is empty and `self_peer_id == content.to`, it looks up `pending_delivered` using the spoofed `content.from`:

```rust
// connection_sync.rs L111-115
let listens_info = self
    .protocol
    .pending_delivered
    .get(&content.from)
    .map(|info| info.0.clone());
```

This returns the attacker's IP inserted in Step 1. The code then spawns a NAT traversal task and calls `raw_session` with the attacker's IP:

```rust
// connection_sync.rs L144-160
let control: ServiceAsyncControl = self.p2p_control.clone();
runtime::spawn(async move {
    if let Ok(((stream, addr), _)) = select_ok(tasks).await {
        let _ignore = control
            .raw_session(stream, addr, RawSessionInfo::inbound(listen_addr))
            .await;
    }
});
```

**Rate limiter analysis:**

The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)`. `ConnectionRequest` has `item_id = 0` and `ConnectionSync` has `item_id = 2` (confirmed in generated code). They consume separate rate limiter slots and can both be sent within the same second. The per-session `rate_limiter` allows 30 req/sec per `(session_id, item_id)` pair — far more than needed for this two-message attack sequence.

**`try_nat_traversal` works regardless of `bind_addr`:** Even when `bind_addr` is `None` (non-Linux or `reuse_port_on_linux = false`), the function still creates a socket and connects to the target IP. The `bind_addr` only controls port reuse, not whether the connection is attempted.

## Impact Explanation

The victim node establishes a raw TCP P2P session to an attacker-controlled IP. By repeating the two-message sequence with fresh spoofed `target_peer_id` values (each bypasses the 2-minute cooldown), the attacker can fill the victim's outbound connection slots with attacker-controlled nodes. This is a targeted eclipse attack: the victim's view of the network is dominated by attacker-controlled peers, enabling selective withholding or manipulation of block/transaction relay and causing **consensus deviation**. This matches the Critical impact class: "Vulnerabilities which could easily cause consensus deviation."

## Likelihood Explanation

The attack requires only a single legitimate P2P connection to the victim — no special privileges, no key material, no majority hashpower. The spoofed fields (`from`, `listen_addrs`) are accepted verbatim from the wire message with no cryptographic binding to the sending session. The two-message sequence is sufficient to trigger `raw_session` to an arbitrary attacker-controlled IP. The attack is repeatable with different spoofed peer IDs to fill all connection slots.

## Recommendation

In `ConnectionRequestProcess::execute` (before calling `respond_delivered`), verify that `content.from` matches the actual peer ID of the sending session by looking up `self.peer` in the peer registry:

```rust
let actual_from = self.protocol.network_state
    .peer_registry.read()
    .get_peer(self.peer)
    .and_then(|p| extract_peer_id(&p.connected_addr));
if actual_from.as_ref() != Some(&content.from) {
    return StatusCode::InvalidFromPeerId.with_context("from does not match sender");
}
```

Similarly, `ConnectionSyncProcess` should be extended with a `peer: PeerIndex` field (passed from `context.session.id` in `mod.rs`) and verify that `content.from` matches the actual sending peer before consuming `pending_delivered`.

## Proof of Concept

```
Setup:
  - Attacker connects to victim (legitimate session, session_id=S, attacker_peer_id=A)
  - Attacker controls a listener at attacker_ip:port

Step 1: Attacker sends ConnectionRequest {
    from         = T  (arbitrary peer ID, T != A),
    to           = victim_peer_id,
    listen_addrs = [/ip4/attacker_ip/tcp/port/p2p/T],
    max_hops     = 1,
    route        = []
}
→ Victim: self_peer_id == content.to → calls respond_delivered(T, ...)
→ Victim: pending_delivered[T] = ([/ip4/attacker_ip/tcp/port/p2p/T], now)
→ Victim: sends ConnectionRequestDelivered back to session S (attacker receives it, ignores it)

Step 2: Attacker sends ConnectionSync {
    from  = T,
    to    = victim_peer_id,
    route = []
}
→ Victim: route is empty, self_peer_id == content.to
→ Victim: listens_info = pending_delivered[T] = [/ip4/attacker_ip/tcp/port/p2p/T]
→ Victim: spawns try_nat_traversal(bind_addr, /ip4/attacker_ip/tcp/port/p2p/T)
→ Attacker's listener accepts the TCP connection
→ Victim: calls raw_session(stream, attacker_ip, RawSessionInfo::inbound(listen_addr))
→ New P2P session established to attacker-controlled node

Step 3: Repeat steps 1-2 with fresh T values (T1, T2, ...) to fill victim's
        outbound connection table with attacker-controlled nodes.
```