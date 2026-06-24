Audit Report

## Title
Unauthenticated `from` Field in `ConnectionRequest`/`ConnectionSync` Enables Attacker-Controlled NAT Traversal and Eclipse Attack — (`network/src/protocols/hole_punching/component/connection_request.rs`, `connection_sync.rs`)

## Summary
The hole-punching protocol's `ConnectionRequestProcess::respond_delivered` and `ConnectionSyncProcess::execute` accept the `from` field verbatim from the wire message without verifying it matches the actual sending peer's identity. An attacker with a single legitimate P2P connection can spoof `from=arbitrary_peer_id` and `listen_addrs=[attacker_ip]`, causing the victim to insert the attacker's IP into `pending_delivered[arbitrary_peer_id]`. A follow-up spoofed `ConnectionSync` then causes the victim to spawn a NAT traversal task and call `raw_session` to the attacker-controlled endpoint, establishing a new P2P session. Repeating with fresh spoofed peer IDs bypasses the per-key cooldown and can fill the victim's connection table with attacker-controlled nodes, constituting a targeted eclipse attack.

## Finding Description

**Root cause — `ConnectionRequestProcess::execute`:**

When `self_peer_id == &content.to`, the code calls `respond_delivered(content.from, ...)` directly:

```rust
// connection_request.rs L145-147
if self_peer_id == &content.to {
    self.respond_delivered(content.from, &content.to, content.listen_addrs)
        .await
```

`content.from` is parsed from the wire message. The struct `ConnectionRequestProcess` holds `self.peer: PeerIndex` (the actual session ID of the sender, passed in from `mod.rs` L114), but this is never compared against `content.from`. There is no lookup of the actual peer ID for `self.peer` in the peer registry to validate the claim.

**Unconditional insertion into `pending_delivered`:**

After sending the `ConnectionRequestDelivered` reply back to the actual session, the attacker-supplied `from_peer_id` and `remote_listens` (attacker's IP) are inserted unconditionally:

```rust
// connection_request.rs L234-237
let now = unix_time_as_millis();
self.protocol
    .pending_delivered
    .insert(from_peer_id, (remote_listens, now));
```

**Cooldown bypass:**

The only guard is the `HOLE_PUNCHING_INTERVAL` (2 minutes) cooldown keyed by `from_peer_id` (L161-166). Since `from_peer_id` is attacker-controlled, using a fresh spoofed peer ID each time trivially bypasses this check.

**Root cause — `ConnectionSyncProcess::execute`:**

`ConnectionSyncProcess` does not even receive the sender's `PeerIndex` — its constructor signature is:

```rust
// connection_sync.rs L60-74
pub(crate) fn new(
    message: ..., protocol: ..., p2p_control: ...,
    bind_addr: Option<SocketAddr>, msg_item_id: u32,
) -> Self {
```

No `peer: PeerIndex` is passed (compare with `ConnectionRequestProcess::new` which does receive it). The execute path looks up `pending_delivered` using the unverified wire field:

```rust
// connection_sync.rs L111-115
let listens_info = self
    .protocol
    .pending_delivered
    .get(&content.from)
    .map(|info| info.0.clone());
```

This returns the attacker's IP inserted in step 1. The code then spawns a NAT traversal task and calls `raw_session` with the attacker's IP:

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

**Rate limiter insufficiency:**

The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)`. `ConnectionRequest` and `ConnectionSync` have distinct `item_id` values, so they occupy separate rate limiter buckets and both pass within the same second. The per-session `rate_limiter` allows 30 requests/second per `(session_id, item_id)`, which is more than sufficient for the two-message attack sequence.

## Impact Explanation

The victim node establishes a raw TCP session to an attacker-controlled IP. By repeating the two-message sequence with different spoofed `from` peer IDs (each bypasses the 2-minute cooldown), the attacker can fill the victim's outbound connection slots with attacker-controlled nodes. This is a targeted eclipse attack: the victim's view of the network is dominated by attacker-controlled peers, enabling selective withholding or manipulation of block and transaction relay. If the eclipsed node is a miner, it can be made to mine on an attacker-controlled fork, causing **consensus deviation**. This matches the Critical impact class: *Vulnerabilities which could easily cause consensus deviation*.

## Likelihood Explanation

The attack requires only a single legitimate P2P connection to the victim — no special privileges, no cryptographic key material, no majority hashpower. The spoofed fields (`from`, `listen_addrs`) are accepted verbatim from the wire with no cryptographic binding to the sending session. The two-message sequence is sufficient to trigger `raw_session` to an arbitrary attacker-controlled IP. The attacker only needs a listening TCP socket at the specified IP:port for `try_nat_traversal` to succeed and `raw_session` to be called. The cooldown bypass via fresh peer IDs makes the attack repeatable at will.

## Recommendation

In `ConnectionRequestProcess::execute` (before calling `respond_delivered`), look up the actual peer ID of the sending session via the peer registry and assert it equals `content.from`:

```rust
let actual_from_peer_id = self.protocol.network_state
    .peer_registry.read()
    .get_peer(self.peer)
    .and_then(|p| p.peer_id().cloned());
if actual_from_peer_id.as_ref() != Some(&content.from) {
    return StatusCode::InvalidFromPeerId
        .with_context("from does not match actual sender");
}
```

In `ConnectionSyncProcess`, pass the sender's `PeerIndex` into the constructor (mirroring `ConnectionRequestProcess`) and apply the same check before consuming `pending_delivered`. Additionally, consider keying `pending_delivered` by the actual session ID rather than the peer-supplied `from` field.

## Proof of Concept

```
1. Attacker establishes a legitimate P2P connection to victim.
   session_id=S, attacker_peer_id=A

2. Attacker sends ConnectionRequest {
       from         = T  (arbitrary spoofed PeerId, T ≠ A),
       to           = victim_peer_id,
       listen_addrs = [attacker_ip:port],
       max_hops     = 1,
       route        = []
   }
   → victim: self_peer_id == content.to → calls respond_delivered(T, ...)
   → no check that T == peer_id(S)
   → pending_delivered.insert(T, ([attacker_ip:port], now))
   → victim sends ConnectionRequestDelivered back to session S

3. Attacker sends ConnectionSync {
       from  = T,
       to    = victim_peer_id,
       route = []
   }
   → victim: route is empty, self_peer_id == content.to
   → pending_delivered.get(&T) = [attacker_ip:port]
   → spawns try_nat_traversal(bind_addr, attacker_ip:port)
   → attacker's listener accepts the TCP connection
   → victim calls raw_session(stream, attacker_ip, RawSessionInfo::inbound(...))
   → new P2P session established to attacker-controlled node

4. Repeat steps 2–3 with fresh T values (T1, T2, T3, ...) to fill
   victim's connection table with attacker-controlled sessions.
```