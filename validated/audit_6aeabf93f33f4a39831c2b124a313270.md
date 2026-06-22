### Title
Unauthenticated `from` Field in Hole Punching `ConnectionRequest` Enables `pending_delivered` Cache Poisoning and Rate-Limiter Bypass — (`network/src/protocols/hole_punching/component/connection_request.rs`)

### Summary

The CKB hole punching protocol accepts a `from` peer ID field directly from the message payload without verifying it matches the actual P2P session sender. Any connected peer can spoof an arbitrary `from` peer ID. This allows an attacker to poison the `pending_delivered` cache under a victim's peer ID, blocking the victim's NAT traversal for up to 2 minutes per injection, and to bypass the `forward_rate_limiter` by rotating spoofed `from` values to amplify message flooding across the network.

---

### Finding Description

The `ConnectionRequest` message schema carries a `from: Bytes` field (the originating peer's ID) as a message-level parameter: [1](#0-0) 

When a relay node receives this message, `ConnectionRequestProcess::execute()` parses `content.from` directly from the payload: [2](#0-1) 

At no point is `content.from` compared against the actual session sender. The `peer` field (the real `PeerIndex` of the sender) is available in the struct: [3](#0-2) 

But `self.peer` is never used to authenticate `content.from`. The two security-sensitive uses of the unauthenticated `content.from` are:

**1. `forward_rate_limiter` keyed on attacker-controlled `from`:** [4](#0-3) 

The limiter key is `(content.from, content.to, msg_item_id)`. Since `content.from` is fully attacker-controlled, the attacker rotates it across requests to generate unlimited unique keys, bypassing the 1-req/sec forward rate limit entirely.

**2. `pending_delivered` cache written under attacker-supplied `from`:** [5](#0-4) [6](#0-5) 

When the relay node is the `to` target, it calls `respond_delivered(content.from, ...)`, which:
- Checks `pending_delivered.get(&from_peer_id)` — if an entry exists within `HOLE_PUNCHING_INTERVAL` (2 minutes), the request is silently ignored
- Inserts `pending_delivered[from_peer_id] = (attacker_listen_addrs, now)`

The `pending_delivered` map is also consumed by `ConnectionSyncProcess::execute()`: [7](#0-6) 

This lookup uses `content.from` from the `ConnectionSync` message — also unauthenticated — to retrieve the listen addresses used for NAT traversal.

---

### Impact Explanation

**Cache poisoning / DoS against victim's hole punching:**
An attacker sends a `ConnectionRequest` with `from=victim_peer_id`, `to=relay_peer_id`, `listen_addrs=[attacker_addrs]`. The relay stores `pending_delivered[victim_peer_id] = (attacker_addrs, now)`. For the next `HOLE_PUNCHING_INTERVAL` (2 minutes), any legitimate `ConnectionRequest` from the real victim to the same relay is silently dropped: [8](#0-7) 

The victim's NAT traversal attempt is blocked. The attacker can re-inject before expiry to maintain the block indefinitely.

**NAT traversal misdirection:**
When a `ConnectionSync` arrives with `from=victim_peer_id`, the relay looks up `pending_delivered[victim_peer_id]` and attempts TCP hole punching to the attacker-controlled addresses instead of the victim's real addresses: [9](#0-8) 

**Rate-limiter bypass enabling amplified flooding:**
The `forward_rate_limiter` is defined as 1 request per second per `(from, to, item_id)` triple: [10](#0-9) 

By rotating `content.from` across requests, a single attacker session generates unlimited unique keys, bypassing the limiter and flooding relay nodes with forwarded `ConnectionRequest` messages.

---

### Likelihood Explanation

The attack requires only a single authenticated P2P connection to a CKB node that has the `HolePunching` protocol enabled. No special privileges, keys, or majority hash power are needed. The `ConnectionRequest` message is a standard P2P protocol message any peer can send. The attacker only needs to know the victim's peer ID (which is publicly advertised via the discovery protocol) and the relay's peer ID.

---

### Recommendation

In `ConnectionRequestProcess::execute()`, resolve the actual sender's `PeerId` from the session context using the peer registry and assert it equals `content.from` before proceeding:

```rust
// Resolve actual sender peer ID from session
let actual_sender_peer_id = self.protocol.network_state
    .with_peer_registry(|reg| reg.get_peer(self.peer).map(|p| p.connected_addr.clone()));
// Then verify: content.from must match the peer ID of self.peer
```

Alternatively, replace `content.from` in all security-sensitive operations (`pending_delivered` insert, rate limiter key) with the authenticated session-derived peer ID, analogous to replacing a caller-supplied `from` address with `msg.sender` in Solidity.

Apply the same fix to `ConnectionRequestDeliveredProcess` and `ConnectionSyncProcess`, which have the same pattern. [11](#0-10) [12](#0-11) 

---

### Proof of Concept

1. Attacker (`A`) connects to relay node `R` via the CKB P2P network.
2. `A` learns victim `V`'s peer ID from the discovery protocol.
3. `A` sends a `ConnectionRequest` message to `R` with:
   - `from = V.peer_id` (spoofed)
   - `to = R.peer_id`
   - `listen_addrs = [A's TCP address]`
4. `R` processes the message in `ConnectionRequestProcess::execute()`. Since `self_peer_id == &content.to`, it calls `respond_delivered(V.peer_id, R.peer_id, [A's addrs])`.
5. `R` inserts `pending_delivered[V.peer_id] = ([A's addrs], now)`.
6. `V` now sends a legitimate `ConnectionRequest` with `from=V.peer_id`, `to=R.peer_id`. `R` checks `pending_delivered.get(&V.peer_id)`, finds the entry inserted in step 5, and since `now - t < HOLE_PUNCHING_INTERVAL` (2 min), returns `StatusCode::Ignore` — **V's hole punching is silently blocked**.
7. `A` re-sends the spoofed message every ~2 minutes to maintain the block indefinitely.
8. For the rate-limiter bypass: `A` sends repeated `ConnectionRequest` messages each with a freshly generated random `from` peer ID. Each generates a unique `(from, to, item_id)` key, bypassing the 1-req/sec `forward_rate_limiter`, causing `R` to forward all of them to downstream peers. [13](#0-12) [14](#0-13)

### Citations

**File:** util/gen-types/schemas/protocols.mol (L94-105)
```text
table ConnectionRequest {
    // Peer Id.
    from: Bytes,
    // Peer Id.
    to: Bytes,
    // Limit the max count of hops: the max count of peers in the message delivery route.
    max_hops: byte,
    // The message delivery route (type: `Vec<PeerId>`).
    route: BytesVec,
    // These are the addresses on which the "from" peer is listening as multi-addresses.
    listen_addrs: AddressVec,
}
```

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L110-153)
```rust
    pub(crate) async fn execute(mut self) -> Status {
        let content = match RequestContent::try_from(&self.message) {
            Ok(content) => content,
            Err(status) => return status,
        };
        if content.listen_addrs.len() > ADDRS_COUNT_LIMIT || content.listen_addrs.is_empty() {
            return StatusCode::InvalidListenAddrLen
                .with_context("the listen address count is too large or empty");
        }

        if content.max_hops > MAX_HOPS {
            return StatusCode::InvalidMaxTTL.into();
        }
        if content.route.len() > MAX_HOPS as usize {
            return StatusCode::InvalidRoute.with_context("the route length is too long");
        }

        let self_peer_id = self.protocol.network_state.local_peer_id();
        if content.route.contains(self_peer_id) {
            return StatusCode::Ignore.with_context("the message is passed, ignore it");
        }

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

        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
        } else if content.max_hops == 0u8 {
            StatusCode::ReachedMaxHops.into()
        } else {
            self.forward_message(self_peer_id, &content.to).await
        }
    }
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-238)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));

```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L85-96)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
        {
            debug!(
                "from: {}, to {}, item_name: {}, rate limit is reached",
                content.from, content.to, "ConnectionSync",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionSync");
        }
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-115)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L117-128)
```rust
                    match listens_info {
                        Some(listens) => {
                            let tasks = listens
                                .into_iter()
                                .map(|listen_addr| {
                                    Box::pin(try_nat_traversal(self.bind_addr, listen_addr))
                                })
                                .collect::<Vec<_>>();

                            if tasks.is_empty() {
                                return StatusCode::Ignore.with_context("no valid listen address");
                            }
```

**File:** network/src/protocols/hole_punching/mod.rs (L23-46)
```rust
pub(crate) const MAX_HOPS: u8 = 6;
pub(crate) const HOLE_PUNCHING_INTERVAL: u64 = 2 * 60 * 1000; // 2 minutes
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes

type PendingDeliveredInfo = (Vec<Multiaddr>, u64);
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;

/// Hole Punching Protocol
pub(crate) struct HolePunching {
    network_state: Arc<NetworkState>,
    bind_addr: Option<SocketAddr>,
    // Request timestamp recorded
    inflight_requests: HashMap<PeerId, u64>,
    // Delivered timestamp recorded
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
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
