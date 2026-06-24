Audit Report

## Title
Unauthenticated `content.from` Field Allows Any Peer to Consume Victim's `inflight_requests` Entry and Redirect NAT Traversal — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

## Summary
In `ConnectionRequestDeliveredProcess::execute`, the branch that handles the "I am the initiator" case (empty `route`, `content.from == local_peer_id`) never validates `content.from` against the actual authenticated session peer ID of the sender. Any connected peer can spoof `content.from = victim_peer_id`, causing the victim to remove a legitimate `inflight_requests` entry and invoke `try_nat_traversal` with attacker-supplied addresses, permanently cancelling the intended hole-punch and redirecting the victim's outbound TCP connection attempt to an attacker-controlled endpoint.

## Finding Description
In `execute()`, after parsing and rate-limit checks, the code matches on `content.route.last()`. With an empty `route`, the `None` arm is entered:

```rust
// connection_request_delivered.rs L147–178
match content.route.last() {
    Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
    None => {
        let self_peer_id = self.protocol.network_state.local_peer_id();
        if self_peer_id != &content.from {
            self.forward_delivered(&content.from).await
        } else {
            // attacker reaches here by setting content.from = victim's own peer ID
            let request_start = self.protocol.inflight_requests.remove(&content.to);
            ...
            self.try_nat_traversal(ttl, content.listen_addrs);
        }
    }
}
``` [1](#0-0) 

The branch decision at line 151 compares `self_peer_id` (the victim's own local peer ID) against `content.from` (a field freely set by the remote sender). The struct holds `self.peer: PeerIndex` — the actual authenticated session ID of the sender — but it is never consulted in this branch. [2](#0-1) 

The only guard is `forward_rate_limiter` keyed on `(content.from, content.to, msg_item_id)`. Because the attacker controls all three fields and each `inflight_requests` entry is consumed on the first hit, a single message per entry is sufficient to bypass it. [3](#0-2) 

The victim's `inflight_requests` entries are observable: `ConnectionRequest` gossip broadcasts (via `filter_broadcast`) expose the `to_peer_id` values being inserted. [4](#0-3) 

**Exploit flow:**
1. Attacker establishes a standard P2P connection to the victim; learns victim's peer ID via the Identify handshake.
2. Attacker observes gossip `ConnectionRequest` broadcasts to enumerate `target_peer_id` values in `inflight_requests`.
3. Attacker sends `ConnectionRequestDelivered` with `from = victim_peer_id`, `to = target_peer_id`, `route = []`, `listen_addrs = [attacker_ip:port]` (the parser appends `/p2p/target_peer_id` automatically per lines 57–63).
4. Victim's `execute()`: `self_peer_id == content.from` → enters `else` block → `inflight_requests.remove(&content.to)` consumes the entry → `try_nat_traversal` spawns an outbound TCP task toward the attacker's address.
5. The legitimate `ConnectionRequestDelivered` that arrives later hits `None => StatusCode::Ignore` at line 175 and is silently dropped. [5](#0-4) 

## Impact Explanation
An attacker with a single standard P2P connection can, at negligible cost, cancel every hole-punch the victim initiates by consuming each `inflight_requests` entry before the legitimate delivery arrives. Scaled across multiple victim nodes simultaneously, this persistently prevents NAT-ed nodes from establishing new outbound connections via the hole-punching mechanism, degrading network connectivity and constituting a low-cost, repeatable disruption of the CKB P2P network. Additionally, `try_nat_traversal` causes the victim to open raw TCP connections toward attacker-controlled IP:port pairs, which can be used for further exploitation of the P2P handshake layer. This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion / connectivity degradation with few costs**.

## Likelihood Explanation
- Requires only a standard P2P connection — no special role, key, or privilege.
- Victim's local peer ID is public (Identify handshake).
- `to_peer_id` values are observable from gossip `ConnectionRequest` broadcasts.
- One well-formed message per target entry suffices; the rate limiter does not prevent it.
- Fully scriptable and repeatable for as long as the attacker remains connected.

## Recommendation
In the `None` arm, before comparing `self_peer_id` against `content.from`, resolve the actual peer ID of the sending session from `self.peer` (the authenticated `PeerIndex`) and reject any message where `content.from` claims to be the local peer ID but the actual sender is not the local node:

```rust
// Resolve authenticated peer ID from session
let sender_peer_id = self.protocol.network_state
    .peer_registry.read()
    .get_peer(self.peer)
    .map(|p| p.peer_id.clone());

if content.from == *self_peer_id {
    match sender_peer_id {
        Some(ref sid) if sid == self_peer_id => { /* legitimate self-loop, proceed */ }
        _ => return StatusCode::Ignore.with_context("spoofed from field"),
    }
}
```

Alternatively, enforce globally that `content.from` must always equal the authenticated peer ID of the direct sender for the first hop, rejecting any mismatch before the route-matching logic.

## Proof of Concept
```rust
// Setup: victim has an inflight entry for `target_peer_id`
protocol.inflight_requests.insert(target_peer_id.clone(), unix_time_as_millis());

// Attacker crafts spoofed message
let spoofed_msg = build_connection_request_delivered(
    /*from=*/        victim_local_peer_id.clone(),  // spoofed
    /*to=*/          target_peer_id.clone(),
    /*route=*/       vec![],                         // empty → None arm
    /*listen_addrs=*/vec![attacker_multiaddr],        // attacker-controlled IP:port
);

// Deliver from attacker's session (attacker_session_id ≠ victim's session)
let status = ConnectionRequestDeliveredProcess::new(
    spoofed_msg.as_reader(), &mut protocol, &control,
    attacker_session_id, bind_addr, item_id,
).execute().await;

// Entry is consumed; try_nat_traversal fires toward attacker address
assert!(protocol.inflight_requests.is_empty());

// Legitimate delivery now silently ignored
let legit = /* same target_peer_id */.execute().await;
assert_eq!(legit, StatusCode::Ignore); // "the request is not in flight"
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L92-99)
```rust
pub struct ConnectionRequestDeliveredProcess<'a> {
    message: packed::ConnectionRequestDeliveredReader<'a>,
    protocol: &'a mut HolePunching,
    p2p_control: &'a ServiceAsyncControl,
    peer: PeerIndex,
    bind_addr: Option<SocketAddr>,
    msg_item_id: u32,
}
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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L147-179)
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
                }
            }
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
