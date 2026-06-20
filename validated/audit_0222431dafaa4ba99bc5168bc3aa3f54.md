### Title
Unauthenticated `from` Field in `ConnectionRequestDelivered` Allows Any Connected Peer to Cancel In-Flight Hole-Punching Requests — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

---

### Summary

`ConnectionRequestDeliveredProcess::execute` removes an entry from `inflight_requests` when it receives a `ConnectionRequestDelivered` message whose `from` field equals the local node's own peer ID and whose `route` is empty. Because the `from` field is parsed from raw message bytes with no cryptographic authentication, any connected peer can forge it to equal the victim's peer ID, triggering the removal and silently discarding the legitimate in-flight request.

---

### Finding Description

When a node initiates a hole-punching request, it records the target peer ID in `inflight_requests`: [1](#0-0) 

When a `ConnectionRequestDelivered` message arrives and the local node determines it is the originator (`self_peer_id == &content.from`) with an empty `route`, it unconditionally removes the entry: [2](#0-1) 

The `from` field is extracted from the raw message bytes with only a structural validity check — no signature, no proof of identity: [3](#0-2) 

An attacker who is a connected peer can craft a `ConnectionRequestDelivered` with:
- `from` = victim's own public peer ID (always observable on the P2P network)
- `to` = the target peer ID (observable from the gossip-broadcast `ConnectionRequest`)
- `route` = empty (forces the `self_peer_id == &content.from` branch)
- `listen_addrs` = any valid TCP multiaddr (to pass the non-empty check at line 125)
- `sync_route` = empty

This causes `inflight_requests.remove(&content.to)` to fire. When the legitimate `ConnectionRequestDelivered` subsequently arrives, the entry is gone and the code returns `Ignore` at line 175, silently discarding the real response and aborting the NAT traversal.

The `forward_rate_limiter` (keyed on `(from, to, item_id)`, 1/sec) does not prevent this: the attacker only needs one successful delivery before the legitimate response arrives. [4](#0-3) 

---

### Impact Explanation

A connected attacker can persistently cancel every hole-punching attempt made by a victim node. For nodes behind NAT that depend on hole-punching for outbound connectivity, this prevents them from establishing new peer connections, degrading or eliminating their ability to participate in the P2P network. Existing connections are unaffected, but the node cannot grow its peer set via NAT traversal.

---

### Likelihood Explanation

- The victim's peer ID is public.
- The target peer ID is observable from the gossip-broadcast `ConnectionRequest` (sent to `sqrt(total)` peers).
- The attacker only needs one established P2P session with the victim.
- No special privileges, keys, or majority hashpower are required.
- The attack is repeatable every 5-minute retry cycle.

---

### Recommendation

Authenticate the `from` field. The simplest fix is to verify that the `from` peer ID in a `ConnectionRequestDelivered` message matches the actual session peer ID of the sender (`self.peer`), or alternatively verify it against the known route recorded when the original `ConnectionRequest` was sent. Without a cryptographic binding between the message's `from` field and the sending session, the field is fully attacker-controlled.

---

### Proof of Concept

1. Node V initiates a hole-punch to peer T. `inflight_requests` now contains `T → timestamp`.
2. Attacker A (connected to V) observes the gossip `ConnectionRequest` with `from=V, to=T`.
3. A sends V a `ConnectionRequestDelivered` with `from=V_peer_id, to=T_peer_id, route=[], sync_route=[], listen_addrs=[<valid TCP addr>]`.
4. V processes the message: `self_peer_id == content.from` is true, `route` is empty → `inflight_requests.remove(&T)` fires.
5. The legitimate `ConnectionRequestDelivered` from T arrives. `inflight_requests.remove(&T)` returns `None` → `StatusCode::Ignore` → NAT traversal never starts. [5](#0-4)

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L239-242)
```rust
            let now = unix_time_as_millis();
            for peer_id in inflight {
                self.inflight_requests.insert(peer_id, now);
            }
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L38-42)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
            .map_err(|_| StatusCode::InvalidToPeerId.with_context("the to peer id is invalid"))?;
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L134-145)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
        {
            debug!(
                "from: {}, to {}, item_name: {}, rate limit is reached",
                content.from, content.to, "ConnectionRequestDelivered",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionRequestDelivered");
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L150-176)
```rust
                let self_peer_id = self.protocol.network_state.local_peer_id();
                if self_peer_id != &content.from {
                    // forward the message to the `from` peer
                    self.forward_delivered(&content.from).await
                } else {
                    // the current peer is the target peer, respond the sync back
                    if let Some(metrics) = ckb_metrics::handle() {
                        metrics.ckb_hole_punching_active_count.inc();
                    }

                    let request_start = self.protocol.inflight_requests.remove(&content.to);

                    match request_start {
                        Some(start) => {
                            let res = self.respond_sync(content.from).await;
                            if !res.is_ok() {
                                return res;
                            }
                            let now = unix_time_as_millis();
                            let ttl = now - start;

                            self.try_nat_traversal(ttl, content.listen_addrs);

                            Status::ok()
                        }
                        None => StatusCode::Ignore.with_context("the request is not in flight"),
                    }
```
