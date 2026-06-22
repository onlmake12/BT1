Looking at the exact code path in `connection_request_delivered.rs` and `mod.rs`:

### Title
Unauthenticated `from` Field in `ConnectionRequestDelivered` Allows Any Peer to Evict Victim's `inflight_requests` Entry — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

---

### Summary

`ConnectionRequestDeliveredProcess::execute` decides whether the local node is the final destination of a `ConnectionRequestDelivered` message by comparing `self_peer_id` against the message's `content.from` field. Because `content.from` is attacker-controlled and is never verified against the actual session's authenticated PeerId, any connected peer can forge `from = victim_peer_id`, trigger the "I am the originator" branch, and call `inflight_requests.remove(&content.to)` — consuming the victim's in-flight hole-punch state before the legitimate response arrives.

---

### Finding Description

The routing decision in `execute()` is:

```
match content.route.last() {
    Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
    None => {
        let self_peer_id = self.protocol.network_state.local_peer_id();
        if self_peer_id != &content.from {
            self.forward_delivered(&content.from).await   // forward
        } else {
            // ← attacker reaches here by setting from = victim's own PeerId
            let request_start = self.protocol.inflight_requests.remove(&content.to);
            ...
        }
    }
}
``` [1](#0-0) 

The `from` field is parsed from the wire message and placed into `DeliverdContent` with no cross-check against the session's actual peer identity: [2](#0-1) 

The only guards before the `remove` call are:
- `listen_addrs` non-empty and ≤ 24 (trivially satisfied by the attacker)
- `route`/`sync_route` length ≤ `MAX_HOPS` (trivially satisfied with empty vectors)
- A rate limiter keyed on `(content.from, content.to, msg_item_id)` — bypassable by varying `msg_item_id` or simply sending once per target [3](#0-2) 

`inflight_requests` is populated in `notify()` when the victim broadcasts a `ConnectionRequest` for a NAT peer: [4](#0-3) 

---

### Impact Explanation

Once the attacker removes the entry, the legitimate `ConnectionRequestDelivered` that eventually arrives from the real relay path hits line 175 (`StatusCode::Ignore`) and is silently discarded: [5](#0-4) 

Additionally, `try_nat_traversal` is invoked with the attacker's fake `listen_addrs`, wasting a 30-second TCP retry loop against attacker-controlled addresses: [6](#0-5) 

The net effect is silent, permanent cancellation of the victim's outbound hole-punch attempt for the targeted peer.

---

### Likelihood Explanation

The most reliable attacker is the `to` peer itself: if the victim is trying to hole-punch to the attacker, the attacker already knows their own PeerId is in the victim's `inflight_requests`. The victim's PeerId is publicly advertised via the Identify protocol. The attacker needs only one connected session to the victim to deliver the crafted message. No special privileges, no PoW, no key material required.

---

### Recommendation

Before entering the "I am the originator" branch, verify that `content.from` matches the **actual session's authenticated PeerId**, not the message's self-reported field:

```rust
// After parsing content, before the routing match:
let session_peer_id = self.protocol.network_state
    .peer_registry.read()
    .get_peer(self.peer)
    .and_then(|p| p.peer_id.clone());

if session_peer_id.as_ref() != Some(&content.from) {
    // sender is lying about `from`; reject or forward only
}
```

Alternatively, enforce that the `from` field in a `ConnectionRequestDelivered` must equal the PeerId of the direct sender (the peer who originally sent the `ConnectionRequest`), which is already known and authenticated at the session layer.

---

### Proof of Concept

1. Victim V has PeerId `V_id` and has an active `inflight_requests` entry for attacker's PeerId `A_id` (V is trying to hole-punch to A).
2. Attacker A is connected to V on the HolePunching protocol.
3. A sends a `ConnectionRequestDelivered` message to V with:
   - `from = V_id` (victim's own PeerId)
   - `to = A_id` (attacker's own PeerId, present in victim's `inflight_requests`)
   - `route = []` (empty)
   - `sync_route = []` (empty)
   - `listen_addrs = [<any valid TCP multiaddr>]`
4. V's `execute()` sees `route.last() == None`, then `self_peer_id == content.from`, and calls `inflight_requests.remove(&A_id)` — entry is gone.
5. The real `ConnectionRequestDelivered` relayed through the network arrives later; V returns `StatusCode::Ignore` and the hole-punch fails permanently.

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L38-41)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L125-145)
```rust
        if content.listen_addrs.len() > ADDRS_COUNT_LIMIT || content.listen_addrs.is_empty() {
            return StatusCode::InvalidListenAddrLen
                .with_context("the listen address count is too large or empty");
        }

        if content.route.len() > MAX_HOPS as usize || content.sync_route.len() > MAX_HOPS as usize {
            return StatusCode::InvalidRoute.with_context("the route length is too long");
        }

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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L147-160)
```rust
        match content.route.last() {
            Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
            None => {
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
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L171-171)
```rust
                            self.try_nat_traversal(ttl, content.listen_addrs);
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L174-176)
```rust
                        }
                        None => StatusCode::Ignore.with_context("the request is not in flight"),
                    }
```

**File:** network/src/protocols/hole_punching/mod.rs (L239-242)
```rust
            let now = unix_time_as_millis();
            for peer_id in inflight {
                self.inflight_requests.insert(peer_id, now);
            }
```
