Now I have all the code I need. Let me trace the full attack path precisely.

### Title
Unauthenticated `from` Field in `ConnectionRequest` Enables `pending_delivered` Cache Poisoning — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

`ConnectionRequestProcess::execute` accepts the `from` PeerId entirely from the message payload and never verifies it matches the actual sending peer's session identity. When the victim node is the `to` target, it unconditionally writes `pending_delivered[content.from] = (content.listen_addrs, now)`. An attacker with any P2P session to the victim can spoof `from=real_peer_id` and inject attacker-controlled listen addresses into the cache. When the legitimate `ConnectionSync{from=real_peer_id}` subsequently arrives, `ConnectionSyncProcess::execute` reads the poisoned entry and spawns `try_nat_traversal` against attacker-controlled endpoints.

---

### Finding Description

**Root cause — no sender authentication on `from`:**

`RequestContent::try_from` parses `from` directly from the wire message: [1](#0-0) 

There is no check that `content.from == peer_id_of(self.peer)`. The actual session identity (`self.peer`, a `PeerIndex`) is never compared against `content.from`.

**Cache write path:**

When `self_peer_id == &content.to`, `respond_delivered` is called: [2](#0-1) 

Inside `respond_delivered`, the only guard is a time-window check: [3](#0-2) 

If the existing entry is older than `HOLE_PUNCHING_INTERVAL` (2 minutes), the check passes and the entry is overwritten: [4](#0-3) 

`remote_listens` here is the attacker-supplied `content.listen_addrs`, filtered only for TCP/IP format — not for ownership.

**Consumption path — `ConnectionSync`:** [5](#0-4) 

The lookup key is `content.from` from the `ConnectionSync` message — again unauthenticated, but in the legitimate flow this is the real peer's PeerId. The poisoned entry is returned and used directly: [6](#0-5) 

**`try_nat_traversal` resource cost:**

Each spawned task retries TCP connections for up to 30 seconds at ~200 ms intervals (~150 attempts per address): [7](#0-6) 

---

### Impact Explanation

- **Connection redirection**: The victim's NAT traversal attempts go to attacker-controlled endpoints instead of the real peer's addresses. Even though the P2P handshake will fail (the attacker lacks the real peer's private key), the victim wastes up to 30 seconds of TCP connection attempts per poisoned entry.
- **Legitimate hole-punching disruption**: The real peer's `ConnectionSync` triggers traversal to wrong addresses; the legitimate NAT traversal never succeeds.
- **Scalable resource exhaustion**: The attacker can spoof many distinct `from` PeerIds (each a different valid-format PeerId bytes) in rapid succession. The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)` — a new spoofed `from` bypasses it entirely. The per-session `rate_limiter` allows 30 messages/second: [8](#0-7) 

A single attacker session can poison 30 distinct `pending_delivered` entries per second, each triggering a 30-second `try_nat_traversal` loop on the victim.

---

### Likelihood Explanation

- Requires only a standard P2P connection to the victim — no special privileges, no PoW, no key material.
- PeerIds of active peers are observable from the gossip network (they appear in forwarded `ConnectionRequest` messages).
- The 2-minute re-entry window is easily satisfied by waiting or by using fresh spoofed PeerIds.
- The attack is repeatable and persistent with minimal bandwidth cost.

---

### Recommendation

In `respond_delivered`, verify that `content.from` matches the actual sending peer's PeerId before writing to `pending_delivered`:

```rust
// Resolve the PeerId of the actual sender from the session registry
let actual_sender_peer_id = self.protocol.network_state
    .peer_registry.read()
    .get_peer(self.peer)
    .map(|p| p.peer_id.clone());

if actual_sender_peer_id.as_ref() != Some(&from_peer_id) {
    return StatusCode::Ignore.with_context("from field does not match actual sender");
}
```

This ensures only the genuine `real_from` peer can write its own addresses into `pending_delivered`. [9](#0-8) 

---

### Proof of Concept

**State-transition sequence:**

1. Legitimate flow at `t0`: `real_from` sends `ConnectionRequest{from=real_from, to=victim, listen_addrs=[real_addrs]}` → victim writes `pending_delivered[real_from] = ([real_addrs], t0)`.

2. Attacker waits until `now - t0 >= HOLE_PUNCHING_INTERVAL` (2 minutes).

3. Attacker (connected to victim as session `S_attacker`) sends:
   ```
   ConnectionRequest {
     from = real_from,   // spoofed
     to   = victim,
     listen_addrs = [attacker_ip:attacker_port]
   }
   ```

4. Victim's `execute()`: `self_peer_id == content.to` → calls `respond_delivered(real_from, victim, [attacker_ip:attacker_port])`.
   - Time check passes (entry is ≥ 2 min old).
   - `send_message_to(S_attacker, ...)` succeeds (attacker is connected).
   - `pending_delivered.insert(real_from, ([attacker_ip:attacker_port], t1))` — entry overwritten.

5. Legitimate `ConnectionSync{from=real_from, to=victim}` arrives via the normal relay path.

6. Victim's `ConnectionSyncProcess::execute()`:
   - `listens_info = pending_delivered.get(&real_from)` → `[attacker_ip:attacker_port]`
   - Spawns `try_nat_traversal(bind_addr, attacker_ip:attacker_port)` — 30-second retry loop against attacker endpoint.

7. Real peer's NAT traversal never completes; victim's resources are consumed on attacker-controlled endpoints.

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L155-167)
```rust
    async fn respond_delivered(
        &mut self,
        from_peer_id: PeerId,
        to_peer_id: &PeerId,
        remote_listens: Vec<Multiaddr>,
    ) -> Status {
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
            }
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-115)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L119-124)
```rust
                            let tasks = listens
                                .into_iter()
                                .map(|listen_addr| {
                                    Box::pin(try_nat_traversal(self.bind_addr, listen_addr))
                                })
                                .collect::<Vec<_>>();
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L65-68)
```rust
    let timeout_duration = Duration::from_secs(30);
    let start_time = Instant::now();
    let mut retry_count = 0u32;
    while start_time.elapsed() < timeout_duration {
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
