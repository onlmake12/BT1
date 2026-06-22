Let me read the component module to understand the full picture.

### Title
Unauthenticated `from` Field in `ConnectionRequestDelivered` Allows Arbitrary `inflight_requests` Eviction — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

### Summary

`ConnectionRequestDeliveredProcess::execute` trusts the attacker-supplied `from` field to decide whether the local node is the originator of a hole-punching request. Because no check verifies that `content.from` matches the actual sender's peer ID, any connected peer can craft a message with `from = local_peer_id` and `route = []` to trigger `inflight_requests.remove(&content.to)` for an arbitrary target peer ID, silently evicting a legitimate in-flight entry.

---

### Finding Description

The routing decision in `execute()` is:

```
route non-empty  →  forward to route.last()
route empty, self_peer_id != content.from  →  forward to content.from
route empty, self_peer_id == content.from  →  remove inflight_requests[content.to]  ← vulnerable branch
``` [1](#0-0) 

The `from` field is parsed directly from the wire message with no binding to the actual session: [2](#0-1) 

The only guards before reaching the destructive branch are:

1. **`listen_addrs` non-empty check** (line 125–128) — trivially satisfied by supplying one valid TCP multiaddr whose embedded peer ID matches `content.to`.
2. **`forward_rate_limiter`** keyed on `(content.from, content.to, msg_item_id)` — allows 1 request/second per tuple, which is far more than needed to evict a single entry. [3](#0-2) 

Neither guard authenticates that `content.from` equals the peer ID of the actual TCP session sending the message.

`inflight_requests` is populated in `notify()` when the local node initiates hole-punching: [4](#0-3) 

Once the entry is removed by the attacker, the legitimate `ConnectionRequestDelivered` that arrives later hits the `None` arm and is silently discarded: [5](#0-4) 

---

### Impact Explanation

The attacker permanently prevents NAT traversal for any target peer whose ID they supply in `content.to`. Because `inflight_requests` entries expire after 5 minutes and are recreated every 2 minutes (`HOLE_PUNCHING_INTERVAL`), the attacker needs to send only one crafted message per renewal cycle per target peer to sustain the DoS indefinitely. This effectively blocks the victim node from establishing outbound connections to NAT-ed peers via the hole-punching protocol. [6](#0-5) 

---

### Likelihood Explanation

- The local node's `PeerId` is publicly advertised via the Identify protocol — no secret knowledge required.
- Any peer that can open a P2P connection (i.e., any node on the network) can send this message.
- The attacker does not need to know the exact contents of `inflight_requests`; they can enumerate known peer IDs from the public peer store, or simply target any peer ID they wish to block.
- The rate limiter does not prevent the attack; one message per second per `(from, to)` tuple is sufficient.

---

### Recommendation

Before entering the `self_peer_id == content.from` branch, verify that the message was received from a peer whose session-level peer ID matches `content.from`. Concretely, look up the peer ID for `self.peer` (the `PeerIndex` of the actual sender) from the peer registry and reject the message if it does not equal `content.from`:

```rust
// pseudocode
let sender_peer_id = self.protocol.network_state
    .peer_registry.read()
    .get_peer(self.peer)
    .map(|p| p.peer_id.clone());

if sender_peer_id.as_ref() != Some(&content.from) {
    return StatusCode::Ignore.with_context("from field does not match sender");
}
``` [7](#0-6) 

---

### Proof of Concept

1. Node A has `local_peer_id = A` and has sent a `ConnectionRequest` for `victim_peer_id = V`, inserting `inflight_requests[V] = now`.
2. Attacker (peer B, connected to A) sends a `ConnectionRequestDelivered` message with:
   - `from = A` (local node's own peer ID, publicly known)
   - `route = []`
   - `to = V`
   - `listen_addrs = [/ip4/1.2.3.4/tcp/8115/p2p/<V>]` (any valid TCP addr for V)
3. `execute()` on node A: `route.last()` → `None`; `self_peer_id (A) == content.from (A)` → true; `inflight_requests.remove(V)` executes.
4. The legitimate `ConnectionRequestDelivered` from V arrives; `request_start` is `None`; returns `Ignore`; NAT traversal never starts.

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L38-40)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
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

**File:** network/src/protocols/hole_punching/mod.rs (L23-28)
```rust
pub(crate) const MAX_HOPS: u8 = 6;
pub(crate) const HOLE_PUNCHING_INTERVAL: u64 = 2 * 60 * 1000; // 2 minutes
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L239-242)
```rust
            let now = unix_time_as_millis();
            for peer_id in inflight {
                self.inflight_requests.insert(peer_id, now);
            }
```
