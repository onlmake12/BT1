### Title
Unauthenticated `from` Field in `ConnectionRequest` Allows Any Peer to Poison `pending_delivered` State and Block Hole-Punching for Arbitrary Peers — (`File: network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

The `HolePunching` protocol's `respond_delivered` function uses the attacker-controlled `content.from` field as the key into the `pending_delivered` map without validating it against the actual authenticated session peer ID. Any connected peer can spoof any victim peer ID in the `from` field of a `ConnectionRequest`, causing the target node to insert a `pending_delivered` entry for the victim. This blocks the legitimate victim peer from receiving a hole-punching response for `HOLE_PUNCHING_INTERVAL` (2 minutes) and poisons the listen-address cache used for NAT traversal.

---

### Finding Description

The `HolePunching` struct maintains two per-peer maps:

- `inflight_requests: HashMap<PeerId, u64>` — timestamps of outbound requests
- `pending_delivered: HashMap<PeerId, PendingDeliveredInfo>` — listen addresses and timestamps of inbound requests that were responded to [1](#0-0) 

When a `ConnectionRequest` arrives and the local node is the `to` target, `respond_delivered` is called with `content.from` as the `from_peer_id`: [2](#0-1) 

Inside `respond_delivered`, the function checks whether a recent entry exists for `from_peer_id` in `pending_delivered`. If the entry is absent or stale, it sends a response and **inserts a new entry keyed by `from_peer_id`**: [3](#0-2) 

The critical flaw: `from_peer_id` comes directly from the message payload (`content.from`). There is **no check that `content.from` matches the authenticated session peer ID** (`self.peer`). The P2P session is authenticated via `secio`, so the real session peer ID is known and trusted, but it is never compared against the `from` field.

When a `ConnectionSync` message later arrives claiming `from = victim_peer_id`, the node looks up `pending_delivered[victim_peer_id]` and uses the stored listen addresses for NAT traversal: [4](#0-3) 

**Attack sequence:**

1. Attacker establishes a legitimate P2P connection to target node N.
2. Attacker sends `ConnectionRequest { from: victim_peer_id, to: N, listen_addrs: [attacker_addr], route: [] }`.
3. Node N sees `self_peer_id == content.to`, calls `respond_delivered(victim_peer_id, N, [attacker_addr])`.
4. No existing entry for `victim_peer_id` → node sends a response to the attacker and inserts `pending_delivered[victim_peer_id] = ([attacker_addr], now)`.
5. For the next `HOLE_PUNCHING_INTERVAL` (2 minutes), any legitimate `ConnectionRequest` from `victim_peer_id` to N returns `StatusCode::Ignore` — the victim is silently blocked.
6. Any subsequent `ConnectionSync` from `victim_peer_id` causes N to attempt NAT traversal to the attacker's address instead of the victim's real address.
7. Attacker repeats step 2 every 2 minutes to maintain the block indefinitely.

The `rate_limiter` (keyed by `(session_id, msg_item_id)`) allows 30 requests/second per session, so the attacker can refresh the block for many victim peer IDs in a single session. [5](#0-4) 

---

### Impact Explanation

An unprivileged peer connected to any CKB node can:

1. **Block hole-punching** for any arbitrary peer ID against the target node, indefinitely and at negligible cost (one message per 2 minutes per victim).
2. **Poison the NAT traversal address cache** (`pending_delivered`) for any peer ID, redirecting NAT traversal attempts to attacker-controlled addresses.

This degrades network connectivity for peers behind NAT that rely on hole punching to join the CKB P2P network. An attacker targeting multiple nodes simultaneously can isolate specific peers from the network.

---

### Likelihood Explanation

- Requires only a single authenticated P2P connection to the target node — no special privileges.
- The `from` field is a plain byte field in the packed message with no cryptographic binding to the session.
- The attack is cheap: one message per 2 minutes per victim peer ID.
- The `forward_rate_limiter` (keyed by `(from, to, msg_item_id)`) does not prevent this because the attacker controls `from` and can vary it freely. [6](#0-5) 

---

### Recommendation

Validate that `content.from` matches the authenticated session peer ID before using it as a key in `pending_delivered`. In `ConnectionRequestProcess::execute`, compare `content.from` against the session's known peer ID (obtainable from the peer registry via `self.peer`):

```rust
// Reject if the from field does not match the actual session peer
let actual_peer_id = self.protocol.network_state
    .peer_registry.read()
    .get_peer(self.peer)
    .and_then(|p| p.connected_addr.peer_id());
if actual_peer_id.as_ref() != Some(&content.from) {
    return StatusCode::InvalidFromPeerId.with_context("from does not match session peer");
}
```

This ensures that only the legitimate owner of a peer ID can trigger `pending_delivered` entries for that peer ID, directly analogous to the recommended fix in the original report (per-user tracking instead of shared state).

---

### Proof of Concept

```
1. Attacker A connects to node N (authenticated session, peer_id = A_id).
2. A sends: ConnectionRequest { from: victim_id, to: N_id, listen_addrs: [A_addr], route: [] }
3. N: content.to == self_peer_id → respond_delivered(victim_id, N_id, [A_addr])
   → pending_delivered[victim_id] = ([A_addr], T)
4. Victim V (peer_id = victim_id) sends: ConnectionRequest { from: victim_id, to: N_id, ... }
5. N: pending_delivered[victim_id] exists, now - T < HOLE_PUNCHING_INTERVAL → returns Ignore
   → V never receives a response; hole punching fails.
6. A repeats step 2 every 119 seconds → V is permanently blocked from hole-punching with N.
``` [7](#0-6) [8](#0-7)

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L24-28)
```rust
pub(crate) const HOLE_PUNCHING_INTERVAL: u64 = 2 * 60 * 1000; // 2 minutes
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L38-47)
```rust
pub(crate) struct HolePunching {
    network_state: Arc<NetworkState>,
    bind_addr: Option<SocketAddr>,
    // Request timestamp recorded
    inflight_requests: HashMap<PeerId, u64>,
    // Delivered timestamp recorded
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
}
```

**File:** network/src/protocols/hole_punching/mod.rs (L95-107)
```rust
        if self
            .rate_limiter
            .check_key(&(session_id, msg.item_id()))
            .is_err()
        {
            debug!(
                "process {} from {}; result is {}",
                item_name,
                session_id,
                status::StatusCode::TooManyRequests.with_context(msg.item_name())
            );
            return;
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L155-240)
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
        let listen_addrs = {
            let public_addr = self.protocol.network_state.public_addrs(ADDRS_COUNT_LIMIT);
            if public_addr.len() < ADDRS_COUNT_LIMIT {
                let observed_addrs = self
                    .protocol
                    .network_state
                    .observed_addrs(ADDRS_COUNT_LIMIT - public_addr.len());
                let iter = public_addr
                    .iter()
                    .chain(observed_addrs.iter())
                    .map(Multiaddr::to_vec)
                    .map(|v| packed::Address::new_builder().bytes(v).build());
                packed::AddressVec::new_builder().extend(iter).build()
            } else {
                let iter = public_addr
                    .iter()
                    .map(Multiaddr::to_vec)
                    .map(|v| packed::Address::new_builder().bytes(v).build());
                packed::AddressVec::new_builder().extend(iter).build()
            }
        };
        let content = init_delivered(self.message, listen_addrs);
        let new_message = packed::HolePunchingMessage::new_builder()
            .set(content)
            .build()
            .as_bytes();
        let proto_id = SupportProtocols::HolePunching.protocol_id();

        let remote_listens: Vec<Multiaddr> = remote_listens
            .into_iter()
            .filter_map(|addr| match find_type(&addr) {
                TransportType::Memory
                | TransportType::Onion
                | TransportType::Ws
                | TransportType::Wss
                | TransportType::Tls => None,
                TransportType::Tcp => {
                    if addr
                        .iter()
                        .any(|p| matches!(p, Protocol::Ip4(_) | Protocol::Ip6(_)))
                    {
                        Some(addr)
                    } else {
                        None
                    }
                }
            })
            .collect();

        if remote_listens.is_empty() {
            return StatusCode::Ignore.with_context("remote listen address is empty");
        }

        debug!(
            "current peer is the target peer {}, send a response back",
            to_peer_id
        );

        if let Err(error) = self
            .p2p_control
            .send_message_to(self.peer, proto_id, new_message)
            .await
        {
            return StatusCode::ForwardError.with_context(error);
        }

        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));

        Status::ok()
    }
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L110-116)
```rust

                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());

```
