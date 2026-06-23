### Title
Unauthenticated `from` Field in `ConnectionRequestDelivered` Allows Any Peer to Consume `inflight_requests` Entry — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

### Summary

`ConnectionRequestDeliveredProcess::execute` decides whether the local node is the originator of a hole-punch request by comparing `self_peer_id` against the message-supplied `content.from` field. Because `content.from` is fully attacker-controlled and is never verified against the actual sender's peer ID, any connected peer can forge a delivery with `from = local_peer_id` and `to = victim_peer_id`, causing `inflight_requests.remove(&content.to)` to consume the entry before the legitimate delivery arrives.

### Finding Description

`inflight_requests` is a `HashMap<PeerId, u64>` keyed by the `to` peer ID of each in-flight hole-punch request. [1](#0-0) 

Entries are inserted in `notify` after broadcasting a `ConnectionRequest`: [2](#0-1) 

When a `ConnectionRequestDelivered` arrives, `execute` reaches the consuming branch when `route` is empty **and** `self_peer_id == content.from`: [3](#0-2) 

The entry is then unconditionally removed: [4](#0-3) 

The `from` field is parsed from raw bytes with no check that it matches the peer ID of the actual TCP/P2P session (`self.peer`): [5](#0-4) 

The only guard is a `forward_rate_limiter` keyed by `(content.from, content.to, msg_item_id)`, which permits **1 spoofed message per second** per unique `(from, to)` pair — far more than the one message needed per 5-minute `CHECK_INTERVAL`: [6](#0-5) [7](#0-6) 

### Impact Explanation

An attacker who is connected to the victim node and knows the victim's local peer ID (public, discoverable via the Identify protocol) and the `to` peer ID (observable from the gossip-broadcast `ConnectionRequest`) can:

1. Send one spoofed `ConnectionRequestDelivered` with `from = local_peer_id`, `to = victim_peer_id`, empty `route`, and any valid `listen_addrs`.
2. The victim's `inflight_requests` entry for `victim_peer_id` is consumed.
3. The legitimate delivery arrives, finds no entry, and returns `StatusCode::Ignore` — no NAT traversal is attempted.
4. Repeating once per 5-minute window permanently prevents hole punching to any targeted peer.

### Likelihood Explanation

- Requires only a standard P2P connection to the victim — no privileges, no keys, no hashpower.
- The `ConnectionRequest` gossip broadcast leaks both `from` and `to` peer IDs to all sqrt(N) recipients, including the attacker.
- The rate limiter (1/s) is not a meaningful barrier; the attack requires only one message per 5-minute cycle.
- The local peer ID is publicly advertised.

### Recommendation

In `execute`, after confirming `self_peer_id == content.from`, verify that the actual sender session (`self.peer`) corresponds to a peer whose peer ID matches `content.from`. Concretely, look up the peer ID for `self.peer` in the peer registry and reject the message if it does not equal `content.from`. This closes the spoofing path without changing the protocol semantics.

### Proof of Concept

```
1. Attacker A connects to victim V (standard P2P).
2. V broadcasts ConnectionRequest{from=V_id, to=T_id, ...} via gossip.
   A receives it and learns V_id and T_id.
3. A sends to V:
     ConnectionRequestDelivered {
       from        = V_id,   // spoofed as local peer ID
       to          = T_id,   // known target
       route       = [],     // empty → triggers the "originator" branch
       sync_route  = [],
       listen_addrs = [<any valid TCP multiaddr>],
     }
4. V's execute():
   - route.last() == None
   - self_peer_id == content.from  ✓  (spoofed)
   - inflight_requests.remove(&T_id) → Some(start)  ← entry consumed
   - respond_sync() sends ConnectionSync back to A (harmless)
5. Legitimate ConnectionRequestDelivered from T arrives:
   - inflight_requests.remove(&T_id) → None
   - returns StatusCode::Ignore; no NAT traversal attempted.
6. Repeat step 3 once every ~5 minutes to permanently block hole punching to T.
```

### Citations

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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L37-42)
```rust
    fn try_from(value: &packed::ConnectionRequestDeliveredReader<'_>) -> Result<Self, Self::Error> {
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
