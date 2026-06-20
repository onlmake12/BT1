The code is fully readable. Here is the analysis.

---

### Title
Unauthenticated `from` Field in `ConnectionRequestDelivered` Allows Inflight Hole-Punching State Poisoning — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

### Summary
An unprivileged peer connected to a victim node can send a spoofed `ConnectionRequestDelivered` message with `from` set to the victim's own peer ID and `to` set to any peer ID present in the victim's `inflight_requests` map. Because the `from` field is never validated against the actual session's peer ID, the victim's node processes the message as if it were a legitimate delivery, removes the `inflight_requests` entry for the target peer, and sends the resulting `ConnectionSync` back to the attacker's session. The legitimate hole-punching attempt to that peer is permanently disrupted for that round.

### Finding Description

The `execute()` function in `ConnectionRequestDeliveredProcess` determines whether the current node is the originator of the hole-punch request by comparing `self_peer_id` (the node's own peer ID) against `content.from` (a field taken directly from the attacker-controlled message): [1](#0-0) 

There is no check that `content.from` matches the actual peer ID of the session that sent the message. A peer ID in the P2P network is public information (exchanged during the identify handshake). The attacker therefore sets `content.from = victim_peer_id`, which passes the equality check and enters the "originator" branch.

Once inside that branch, the victim unconditionally removes the entry for `content.to` from `inflight_requests`: [2](#0-1) 

`respond_sync` is then called, but it sends the `ConnectionSync` to `self.peer` — the attacker's session index, not to the legitimate target peer: [3](#0-2) 

The `inflight_requests` map is a `HashMap<PeerId, u64>` keyed by the target peer ID: [4](#0-3) 

New entries are only inserted during the `notify` tick, which fires every 5 minutes: [5](#0-4) [6](#0-5) 

The `forward_rate_limiter` is keyed on `(content.from, content.to, msg_item_id)` — all attacker-controlled fields — and allows 1 request per second per key: [7](#0-6) 

A single message per 5-minute window is sufficient to consume each new inflight entry as it is created.

The target peer IDs in `inflight_requests` are discoverable: the victim broadcasts `ConnectionRequest` messages to `sqrt(total_peers)` connected peers via gossip, and the attacker (being connected) can observe these broadcasts: [8](#0-7) 

### Impact Explanation
The attacker can continuously consume the victim's `inflight_requests` entries as they are created every 5 minutes, preventing the victim from ever completing hole punching to NAT-traversal peers. The `ConnectionSync` is misdirected to the attacker's session, so the passive peer (X) never receives it and never initiates NAT traversal. This degrades the victim's ability to expand its outbound connections to NAT-ed peers, contributing to network topology fragmentation for the targeted node.

### Likelihood Explanation
Preconditions are minimal: the attacker needs only a single P2P connection to the victim (standard, unprivileged) and knowledge of the victim's peer ID (public) and one peer ID in `inflight_requests` (observable from gossiped `ConnectionRequest` broadcasts). No cryptographic material, privileged access, or majority hashpower is required. The attack is repeatable every 5 minutes with one message per cycle.

### Recommendation
Validate that `content.from` matches the actual peer ID of the session that delivered the message. The session's peer ID is available via the peer registry using `self.peer` (the `PeerIndex`). Before entering the "originator" branch, add a check:

```rust
let sender_peer_id = self.protocol.network_state
    .peer_registry.read()
    .get_peer(self.peer)
    .map(|p| p.peer_id.clone());

if sender_peer_id.as_ref() != Some(&content.from) {
    return StatusCode::Ignore.with_context("from field does not match sender");
}
```

Alternatively, the `from` field could be dropped from the message entirely and inferred from the session identity, since the originator is always the node that initiated the `ConnectionRequest`.

### Proof of Concept

```
1. Victim V has peer ID V_id and has peer X_id in inflight_requests.
2. Attacker A is connected to V with session S_A.
3. A observes V's gossiped ConnectionRequest(from=V_id, to=X_id, ...) and learns X_id.
4. A sends to V: ConnectionRequestDelivered {
       from: V_id,       // spoofed to victim's own peer ID
       to: X_id,         // target in inflight_requests
       route: [],        // empty → triggers "originator" branch
       sync_route: [],
       listen_addrs: [valid_addr/p2p/X_id],
   }
5. V.execute():
   - route.last() == None
   - self_peer_id (V_id) == content.from (V_id) → enters originator branch
   - inflight_requests.remove(&X_id) → returns Some(start), entry deleted
   - respond_sync sends ConnectionSync to S_A (attacker's session)
6. A discards the ConnectionSync.
7. X never receives ConnectionSync; hole punching to X fails.
8. After 5 minutes, V re-inserts X_id into inflight_requests; A repeats step 4.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L150-154)
```rust
                let self_peer_id = self.protocol.network_state.local_peer_id();
                if self_peer_id != &content.from {
                    // forward the message to the `from` peer
                    self.forward_delivered(&content.from).await
                } else {
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L160-160)
```rust
                    let request_start = self.protocol.inflight_requests.remove(&content.to);
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L226-229)
```rust
        if let Err(error) = self
            .p2p_control
            .send_message_to(self.peer, proto_id, new_message)
            .await
```

**File:** network/src/protocols/hole_punching/mod.rs (L25-25)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
```

**File:** network/src/protocols/hole_punching/mod.rs (L42-42)
```rust
    inflight_requests: HashMap<PeerId, u64>,
```

**File:** network/src/protocols/hole_punching/mod.rs (L223-235)
```rust
                    // Broadcast to a number of nodes equal to the square root of the total connection count using gossip.
                    let mut total = status.total.isqrt();
                    let _ignore = context
                        .filter_broadcast(
                            TargetSession::Filter(Box::new(move |_| {
                                total = total.saturating_sub(1);
                                total != 0
                            })),
                            proto_id,
                            conn_req.as_bytes(),
                        )
                        .await;
                    inflight.push(to_peer_id);
```

**File:** network/src/protocols/hole_punching/mod.rs (L239-242)
```rust
            let now = unix_time_as_millis();
            for peer_id in inflight {
                self.inflight_requests.insert(peer_id, now);
            }
```

**File:** network/src/protocols/hole_punching/mod.rs (L256-257)
```rust
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```
