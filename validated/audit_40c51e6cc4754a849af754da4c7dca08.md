Audit Report

## Title
Unauthenticated `from` field in `ConnectionRequest`/`ConnectionSync` enables arbitrary IP injection into `pending_delivered`, triggering attacker-directed NAT traversal — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary
`ConnectionRequestProcess::execute` decodes `content.from` entirely from the message payload with no cross-check against the actual session's peer identity. Combined with a `listen_addrs` guard that only validates the `/p2p/` component against the attacker-supplied `from`, any single connected peer can inject arbitrary `(synthetic_peer_id → attacker_IP)` entries into `pending_delivered`. A follow-up `ConnectionSync` carrying the same spoofed `from` then causes the victim to initiate outbound TCP connections to attacker-controlled addresses, enabling an eclipse attack.

## Finding Description

**Root cause — no sender identity check in `ConnectionRequestProcess::execute`:**

`content.from` is decoded from the wire at `connection_request.rs:36-38` with no cross-check against `self.peer` (the actual `PeerIndex`/session). The struct carries `peer: PeerIndex` at line 88 but it is only used to route the reply (`send_message_to(self.peer, …)` at line 228), never to validate that `content.from` matches the session's real peer_id. [1](#0-0) [2](#0-1) 

**`listen_addrs` guard is trivially satisfied by the attacker:**

The only address validation checks that the `/p2p/` component of each address equals `from` — but `from` is itself attacker-controlled. Setting `from = B` (any synthetic peer_id) and `listen_addrs = [<attacker_IP>/p2p/B]` passes the check unconditionally. [3](#0-2) 

**`respond_delivered` writes attacker-supplied addresses into `pending_delivered`:**

When `self_peer_id == &content.to`, `respond_delivered` is called and unconditionally inserts `(from_peer_id, (remote_listens, now))` into the map. [4](#0-3) 

**`ConnectionSyncProcess::execute` has the same missing check and consumes the poisoned map:**

`content.from` is again decoded from the payload without session validation (`connection_sync.rs:42-44`), then used directly to index `pending_delivered` and spawn `try_nat_traversal` tasks to the stored addresses. `ConnectionSyncProcess` has no `peer` field at all, so there is no session anchor to validate against. [5](#0-4) 

**All dedup/rate guards are keyed on the attacker-controlled `from` field:**

- `forward_rate_limiter` key: `(content.from, content.to, msg_item_id)` — rotating synthetic `from` values bypasses it entirely. [6](#0-5) 
- `HOLE_PUNCHING_INTERVAL` dedup: keyed by `from_peer_id` — rotating `from` bypasses it. [7](#0-6) 
- The only meaningful throttle is `rate_limiter` keyed by `(session_id, msg.item_id())` at 30 req/s. Over the 5-minute `TIMEOUT` window this still permits 9,000 injected entries per session. [8](#0-7) [9](#0-8) 

## Impact Explanation
With one unprivileged P2P connection the attacker saturates `pending_delivered` with `(synthetic_peer_id → attacker_IP)` entries, then sends matching `ConnectionSync` messages to cause the victim to dial attacker-controlled addresses via `try_nat_traversal`. Successful connections consume outbound peer slots. Once all outbound slots are filled with attacker-controlled peers the victim is eclipsed: it receives only attacker-curated blocks, headers, and transactions, enabling consensus deviation (feeding a longer attacker chain, withholding blocks, facilitating double-spends). This matches **Critical — Vulnerabilities which could easily cause consensus deviation**.

## Likelihood Explanation
- Requires only one standard P2P connection; no special role, key material, or hashpower.
- Fully scriptable: generate synthetic Ed25519 keypairs, craft `ConnectionRequest` + `ConnectionSync` pairs with rotating `from` values, send over the existing session.
- The per-session `rate_limiter` (30/s) does not prevent the attack; it only bounds the injection rate to ~9,000 entries over the 5-minute `TIMEOUT` window.
- The `HOLE_PUNCHING_INTERVAL` and `forward_rate_limiter` guards are both bypassed by rotating `from`.
- Repeatable indefinitely until the victim's connection slots are exhausted.

## Recommendation
In `ConnectionRequestProcess::execute` and `ConnectionSyncProcess::execute`, after parsing `content`, look up the actual peer_id for the session from the peer registry (e.g., `network_state.peer_registry.read().get_peer(self.peer).map(|p| p.peer_id.clone())`) and assert it equals `content.from`. Reject and ban the session if they differ. `ConnectionSyncProcess` must be extended with a `peer: PeerIndex` field (mirroring `ConnectionRequestProcess`) to make this check possible. This is the standard pattern used by other CKB protocols that carry a self-identifying `from` field.

## Proof of Concept
```rust
// Attacker is connected to victim T (peer_id = T_id) with session peer_id A.
// Victim's local_peer_id = T_id.

for i in 0..N {
    let b_keypair = Keypair::generate_ed25519();
    let b_peer_id = b_keypair.public().to_peer_id(); // synthetic, never connected

    let mut listen_addr: Multiaddr = "/ip4/1.2.3.4/tcp/9999".parse().unwrap();
    listen_addr.push(Protocol::P2P(Cow::Borrowed(b_peer_id.as_bytes())));

    // Step 1: inject (b_peer_id → 1.2.3.4:9999) into victim's pending_delivered
    let req = packed::ConnectionRequest::new_builder()
        .from(b_peer_id.as_bytes())   // spoofed — not session peer_id A
        .to(T_id.as_bytes())
        .max_hops(6u8)
        .listen_addrs(vec![listen_addr])
        .build();
    send_over_session_A(req); // listen_addrs check passes: /p2p/b_peer_id == from

    // Step 2: trigger try_nat_traversal to 1.2.3.4:9999
    let sync = packed::ConnectionSync::new_builder()
        .from(b_peer_id.as_bytes())   // same spoofed from
        .to(T_id.as_bytes())
        .build();
    send_over_session_A(sync);
    // Victim reads pending_delivered[b_peer_id] = [1.2.3.4:9999] and dials attacker.
}
// After N iterations victim's outbound slots are filled with attacker-controlled peers.
```
Rotate `b_peer_id` each iteration to bypass `HOLE_PUNCHING_INTERVAL` and `forward_rate_limiter`. The per-session `rate_limiter` (30/s) bounds throughput but does not prevent the attack.

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L46-55)
```rust
                    Ok(mut addr) => {
                        if let Some(peer_id) = extract_peer_id(&addr) {
                            if peer_id != from {
                                return Err(StatusCode::InvalidListenAddrLen
                                    .with_context("peer id in listen address is invalid"));
                            }
                        } else {
                            addr.push(Protocol::P2P(Cow::Borrowed(from.as_bytes())));
                        }
                        Ok(addr)
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L85-91)
```rust
pub(crate) struct ConnectionRequestProcess<'a> {
    message: packed::ConnectionRequestReader<'a>,
    protocol: &'a mut HolePunching,
    peer: PeerIndex,
    p2p_control: &'a ServiceAsyncControl,
    msg_item_id: u32,
}
```

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L161-166)
```rust
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
            }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-124)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());

                    match listens_info {
                        Some(listens) => {
                            let tasks = listens
                                .into_iter()
                                .map(|listen_addr| {
                                    Box::pin(try_nat_traversal(self.bind_addr, listen_addr))
                                })
                                .collect::<Vec<_>>();
```

**File:** network/src/protocols/hole_punching/mod.rs (L95-107)
```rust
        if self
            .rate_limiter
            .check_key(&(session_id, msg.item_id()))
            .is_err()
        {
            debug!(
                "process {} from {}; result is {}",
                item_name,
                session_id,
                status::StatusCode::TooManyRequests.with_context(msg.item_name())
            );
            return;
        }
```

**File:** network/src/protocols/hole_punching/mod.rs (L173-174)
```rust
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
```
