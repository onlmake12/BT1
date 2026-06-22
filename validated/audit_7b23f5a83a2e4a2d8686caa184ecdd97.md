### Title
Unauthenticated `from` Field in `ConnectionRequest` Enables `pending_delivered` Poisoning via Identity Spoofing — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

`ConnectionRequestProcess::execute` accepts the `from` field from the wire message verbatim and uses it as the key when writing attacker-controlled addresses into `pending_delivered`. There is no check that `content.from` matches the actual `PeerId` of the session that sent the message. Any connected peer can impersonate an arbitrary `PeerId` and poison the target node's `pending_delivered` map with attacker-controlled listen addresses.

---

### Finding Description

In `ConnectionRequestProcess::execute`, `content.from` is parsed directly from the message bytes: [1](#0-0) 

The `peer` field stored in the process struct is a `PeerIndex` (session ID), not the session's `PeerId`: [2](#0-1) 

In `execute`, after structural validation (address count, max_hops, route length, loop detection, rate limiting), when `self_peer_id == &content.to`, `respond_delivered` is called with the attacker-controlled `content.from`: [3](#0-2) 

Inside `respond_delivered`, the attacker-controlled `from_peer_id` and `remote_listens` (also from the message) are written unconditionally into `pending_delivered`: [4](#0-3) 

At no point is the session's actual `PeerId` looked up from the peer registry and compared against `content.from`. The `peer` field is only used to route the `ConnectionRequestDelivered` reply back to the sender session: [5](#0-4) 

The `pending_delivered` map is later consumed by `ConnectionSyncProcess::execute`. When a `ConnectionSync` arrives with `from=victim_peer_id`, the target node looks up `pending_delivered.get(&content.from)` and initiates NAT traversal to whatever addresses are stored there: [6](#0-5) 

If the attacker has poisoned the entry for `victim_peer_id`, the target will attempt to open a raw TCP session to the attacker's addresses instead of the victim's real addresses: [7](#0-6) 

---

### Impact Explanation

The direct, concrete impact is **address poisoning in `pending_delivered`**: the target node stores attacker-controlled multiaddrs under an arbitrary victim `PeerId`. When a legitimate `ConnectionSync` subsequently arrives for that victim, the target initiates NAT traversal to the attacker's infrastructure rather than the victim's real endpoints. A successful raw session established this way connects the target to the attacker's node under the guise of the victim's identity.

This enables a targeted eclipse attack against the hole-punching path: the attacker can systematically poison entries for all well-known honest peers, causing the target to exhaust its hole-punching connection budget on attacker-controlled endpoints. Eclipse attacks on CKB nodes can cause consensus deviation by feeding the isolated node a forked chain.

The `HOLE_PUNCHING_INTERVAL` (2-minute cooldown) only prevents re-insertion for the same `from_peer_id` within a window: [8](#0-7) 

An attacker can rotate through many victim `PeerId` values to bypass this. The `forward_rate_limiter` is keyed by `(from, to, item_id)`: [9](#0-8) 

Varying any of these three fields bypasses the rate limit entirely.

---

### Likelihood Explanation

The attacker needs only a single authenticated P2P connection to the target node (or any relay node that has a path to the target). No special privileges, no leaked keys, no majority hashpower. The `ConnectionRequest` message is a standard production P2P message. The spoofed `from` field passes all existing validation because the only structural check on `from` is that it decodes as a valid `PeerId`: [1](#0-0) 

The attack is locally testable: connect a test peer with `PeerId A` to a target node, send a `ConnectionRequest` with `from=PeerId B` (any known peer), observe `pending_delivered` contains `B -> attacker_addrs`.

---

### Recommendation

In `ConnectionRequestProcess::execute` (or in `received` before dispatch), look up the session's actual `PeerId` from the peer registry using `self.peer` (the `PeerIndex`) and assert it equals `content.from`. Reject (and optionally ban) the session if they differ. The peer registry already supports this lookup via `get_peer(session_id)`: [10](#0-9) 

For forwarded messages (where `from` is a remote originator, not the immediate sender), the `route` field already records the forwarding path. The fix should only enforce the `from == session PeerId` invariant for the **first hop** (i.e., when `route` is empty), since relay nodes legitimately forward messages on behalf of the original sender.

---

### Proof of Concept

```
1. Attacker node (PeerId=A) establishes a P2P connection to target node T.
2. Attacker sends a ConnectionRequest message:
     from = victim_peer_id (B, a well-known honest node)
     to   = T's own PeerId
     listen_addrs = [attacker_ip:attacker_port/p2p/B]
     max_hops = 1, route = []
3. T's ConnectionRequestProcess::execute sees self_peer_id == content.to,
   calls respond_delivered(B, T, [attacker_addr]).
4. respond_delivered inserts: pending_delivered[B] = ([attacker_addr], now).
5. Later, when a legitimate ConnectionSync{from=B, to=T} arrives (e.g.,
   forwarded from the real peer B or crafted by the attacker),
   ConnectionSyncProcess looks up pending_delivered[B] and calls
   try_nat_traversal(bind_addr, attacker_addr), opening a raw TCP session
   to the attacker's endpoint.
6. The attacker's node completes the handshake, appearing to T as a
   legitimate inbound connection from the hole-punching path.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L226-229)
```rust
        if let Err(error) = self
            .p2p_control
            .send_message_to(self.peer, proto_id, new_message)
            .await
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L143-160)
```rust
                                Some(listen_addr) => {
                                    let control: ServiceAsyncControl = self.p2p_control.clone();
                                    runtime::spawn(async move {
                                        if let Ok(((stream, addr), _)) = select_ok(tasks).await {
                                            debug!("NAT traversal success, addr: {:?}", addr);
                                            if let Some(metrics) = ckb_metrics::handle() {
                                                metrics
                                                    .ckb_hole_punching_passive_success_count
                                                    .inc();
                                            }

                                            let _ignore = control
                                                .raw_session(
                                                    stream,
                                                    addr,
                                                    RawSessionInfo::inbound(listen_addr),
                                                )
                                                .await;
```

**File:** network/src/protocols/hole_punching/mod.rs (L24-24)
```rust
pub(crate) const HOLE_PUNCHING_INTERVAL: u64 = 2 * 60 * 1000; // 2 minutes
```

**File:** network/src/peer_registry.rs (L258-260)
```rust
    pub fn get_peer(&self, session_id: SessionId) -> Option<&Peer> {
        self.peers.get(&session_id)
    }
```
