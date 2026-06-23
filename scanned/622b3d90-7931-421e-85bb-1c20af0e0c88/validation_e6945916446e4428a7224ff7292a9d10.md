### Title
Unauthenticated `from` Field in `ConnectionRequestDelivered` Allows Any Peer to Consume Victim's `inflight_requests` Entry — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

---

### Summary

`ConnectionRequestDeliveredProcess::execute` routes a message to the "I am the initiator" branch solely based on whether `content.from` equals the local peer ID. Because `content.from` is attacker-controlled and never validated against the actual sender's session peer ID, any connected peer can set `from = victim_peer_id`, `route = []`, and a valid `to = target_peer_id` to unconditionally consume the victim's `inflight_requests` entry and redirect NAT traversal to attacker-controlled addresses.

---

### Finding Description

When `execute()` processes a `ConnectionRequestDelivered` message with an empty `route`, it falls into the `None` arm of the `route.last()` match: [1](#0-0) 

The branch decision at line 151 compares `self_peer_id` (the victim's own local peer ID) against `content.from` (a field the remote sender controls freely). There is no check that `content.from` matches the actual session peer ID of the sender. If the attacker sets `from = victim_peer_id`, the condition `self_peer_id != &content.from` is `false`, and execution falls into the `else` block, which immediately calls: [2](#0-1) 

This removes the `target_peer_id` entry from `inflight_requests` and, if the entry existed (`Some(start)`), calls `try_nat_traversal` with the attacker-supplied `listen_addrs`. Any subsequent legitimate `ConnectionRequestDelivered` for the same `target_peer_id` hits the `None` arm at line 175 and is silently ignored.

The victim's `inflight_requests` entries are publicly observable: the victim gossip-broadcasts `ConnectionRequest` messages (via `filter_broadcast`) that contain the `to_peer_id` values being inserted into `inflight_requests`: [3](#0-2) 

The only rate-limiting guard is `forward_rate_limiter` keyed on `(content.from, content.to, msg_item_id)`: [4](#0-3) 

Since each `inflight_requests` entry is consumed on the first hit, one message per entry is sufficient; the rate limiter does not prevent the attack.

---

### Impact Explanation

- **Legitimate hole-punch cancelled**: The real `ConnectionRequestDelivered` that arrives later finds no entry in `inflight_requests` and returns `Ignore`, so the intended NAT traversal never proceeds.
- **Misdirected connection attempt**: `try_nat_traversal` is called with attacker-supplied IP:port (the address has `target_peer_id` appended per the `listen_addrs` parsing at lines 57–63, but the IP and port are fully attacker-controlled). The victim opens a raw TCP connection toward the attacker.
- **Persistent DoS on hole-punching**: An attacker who stays connected can watch gossip for new `ConnectionRequest` broadcasts and immediately cancel every hole-punch the victim initiates, preventing the victim from ever establishing connections to NAT-ed peers via this mechanism.

---

### Likelihood Explanation

- The attacker only needs a standard P2P connection to the victim — no special role or key.
- The victim's local peer ID is public (exchanged during the identify handshake).
- The `to_peer_id` values in `inflight_requests` are observable from gossip `ConnectionRequest` broadcasts.
- The attack requires sending a single well-formed message per target entry; it is trivially scriptable.

---

### Recommendation

Validate `content.from` against the actual sender's authenticated peer ID before entering the "I am the initiator" branch. Concretely, in the `None` arm, reject (or at minimum ignore) any message where `content.from` equals the local peer ID but the sending session's peer ID does not also equal the local peer ID. The simplest fix is to add a check:

```rust
// In the None arm, before the self_peer_id comparison:
let sender_peer_id = /* peer ID resolved from self.peer session */;
if sender_peer_id != self_peer_id && content.from == *self_peer_id {
    return StatusCode::Ignore.with_context("spoofed from field");
}
```

Alternatively, require that `content.from` always matches the actual sender's session peer ID for the entire message, not just in this branch.

---

### Proof of Concept

```rust
// 1. Insert a fake inflight entry as if the victim initiated a hole-punch to `target`
protocol.inflight_requests.insert(target_peer_id.clone(), unix_time_as_millis());

// 2. Craft a ConnectionRequestDelivered with:
//    from  = victim's own local_peer_id
//    to    = target_peer_id
//    route = []  (empty)
//    listen_addrs = [attacker_ip:port/p2p/target_peer_id]  (valid, non-empty)
let spoofed_msg = build_connection_request_delivered(
    /*from=*/ victim_local_peer_id.clone(),
    /*to=*/   target_peer_id.clone(),
    /*route=*/ vec![],
    /*listen_addrs=*/ vec![attacker_addr_with_target_peer_id],
);

// 3. Deliver from attacker's session
let status = ConnectionRequestDeliveredProcess::new(
    spoofed_msg.as_reader(), &mut protocol, &control,
    attacker_session_id, bind_addr, item_id,
).execute().await;

// 4. Assert: inflight_requests is now empty (entry consumed)
assert!(protocol.inflight_requests.is_empty());

// 5. Deliver the legitimate ConnectionRequestDelivered for the same target
let legit_status = /* ... same target_peer_id ... */.execute().await;
// Returns Ignore — "the request is not in flight"
assert_eq!(legit_status, StatusCode::Ignore);
```

### Citations

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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L147-176)
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

**File:** network/src/protocols/hole_punching/mod.rs (L224-241)
```rust
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
                }
            }

            let now = unix_time_as_millis();
            for peer_id in inflight {
                self.inflight_requests.insert(peer_id, now);
```
