The key logic path is fully traceable. Let me confirm the exact check (or lack thereof) on the `from` field vs. the actual session sender.

The attack path is concrete and fully traceable. Here is the analysis:

---

### Title
Unauthenticated `from` Field in `ConnectionRequestDelivered` Allows Arbitrary Removal of `inflight_requests` Entries — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

### Summary
An unprivileged connected peer can forge a `ConnectionRequestDelivered` message with `from` set to the local node's own peer ID and `route=[]`, causing the local node to unconditionally call `inflight_requests.remove(&content.to)` and silently discard a legitimate in-flight hole-punching entry. No authentication of the `from` field against the actual session sender is performed anywhere in the handler.

### Finding Description

The `execute()` method in `ConnectionRequestDeliveredProcess` routes the message based on two conditions:

1. If `route` is non-empty → forward to the last hop.
2. If `route` is empty and `content.from != local_peer_id` → forward to `content.from`.
3. If `route` is empty and `content.from == local_peer_id` → treat as final delivery, call `inflight_requests.remove(&content.to)`. [1](#0-0) 

The `from` field is parsed directly from the attacker-supplied message bytes. There is no check that `content.from` matches the peer ID of the actual session sender (`self.peer`). The `peer` field (the real session's `PeerIndex`) is present in the struct but is only used in `respond_sync` to send a reply back to the attacker — it is never compared against `content.from`. [2](#0-1) 

The only rate-limiting guard is the `forward_rate_limiter` keyed on `(content.from, content.to, msg_item_id)`: [3](#0-2) 

This allows 1 request per second per `(from, to)` pair. Since `inflight_requests` entries are inserted only once every 5 minutes (in the `notify` timer): [4](#0-3) [5](#0-4) 

the attacker only needs to send one spoofed message per 5-minute window per victim peer ID to keep the entry permanently removed. The rate limiter (1/sec) does not prevent this.

`inflight_requests` has exactly one insert site and one remove site: [6](#0-5) 

### Impact Explanation

When the legitimate `ConnectionRequestDelivered` arrives after the attacker's spoofed one, `inflight_requests.remove(&content.to)` returns `None`, and the handler returns `StatusCode::Ignore` without attempting NAT traversal: [7](#0-6) 

The result is that hole-punching is silently aborted for the targeted peer. The attacker, by observing `ConnectionRequest` gossip broadcasts (which reveal the `to` peer IDs being targeted), can learn exactly which entries to remove and suppress all NAT traversal attempts on any node they are connected to.

### Likelihood Explanation

- Peer IDs are public and discoverable via the P2P discovery protocol.
- `ConnectionRequest` messages are broadcast via `filter_broadcast` to a square-root subset of connected peers, so a connected attacker will routinely observe them and learn the `to` peer IDs.
- The attack requires only a single connected session and one message per 5-minute window per victim peer ID.
- No cryptographic material, privileged access, or majority hashpower is needed.

### Recommendation

In `execute()`, before entering the `self_peer_id == content.from` branch, verify that the actual session sender's peer ID matches `content.from`. The session's peer ID can be resolved from `self.peer` via the peer registry. If they do not match, the message should be rejected (or forwarded, not locally processed). This closes the spoofing vector entirely.

### Proof of Concept

1. Node A has `local_peer_id = A` and has an entry `inflight_requests[X] = t` (inserted during `notify`).
2. Attacker (connected peer B) observes the `ConnectionRequest` broadcast and learns `to = X`.
3. Attacker sends `ConnectionRequestDelivered { from: A, to: X, route: [], listen_addrs: [valid_addr], sync_route: [] }`.
4. Node A's handler: `route.last()` → `None`; `local_peer_id == content.from` → enters the local-processing branch; calls `inflight_requests.remove(&X)` → entry deleted.
5. The legitimate `ConnectionRequestDelivered` for `X` arrives later; `inflight_requests.remove(&X)` returns `None`; handler returns `Ignore`; NAT traversal is never attempted.
6. Repeat once per 5-minute `notify` cycle to permanently suppress hole-punching to `X`.

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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L147-161)
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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L162-176)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L25-28)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L42-42)
```rust
    inflight_requests: HashMap<PeerId, u64>,
```

**File:** network/src/protocols/hole_punching/mod.rs (L239-242)
```rust
            let now = unix_time_as_millis();
            for peer_id in inflight {
                self.inflight_requests.insert(peer_id, now);
            }
```
