### Title
Unverified `from` Field in HolePunching `ConnectionRequest` Enables Rate-Limiter Bypass and `pending_delivered` Map Poisoning - (`network/src/protocols/hole_punching/component/connection_request.rs`)

### Summary

The `ConnectionRequestProcess::execute()` handler in the Hole Punching protocol parses the `from` peer ID entirely from the attacker-controlled message body and uses it directly for rate-limiting and state storage, without ever verifying it against the cryptographically authenticated session peer ID. This is the direct CKB analog of the `InfernalRiftBelow.claimRoyalties` pattern: a check is performed on data *supplied by the caller* rather than on the caller's verified identity. Any connected peer can rotate arbitrary fake `from` values to bypass the `forward_rate_limiter` and poison the `pending_delivered` map with attacker-chosen peer IDs and listen addresses.

### Finding Description

**Root cause — missing identity binding between session and message `from` field**

In `network/src/protocols/hole_punching/component/connection_request.rs`, `RequestContent::try_from` parses `from` directly from the wire message:

```rust
let from = PeerId::from_bytes(value.from().raw_data().to_vec())...
``` [1](#0-0) 

The actual, cryptographically authenticated peer ID of the sender is available from the secio session (passed in as `self.peer` / `context.session.id`), but `execute()` never asserts `content.from == actual_session_peer_id`. The `from` value is then used in two security-sensitive ways:

**1. `forward_rate_limiter` keyed on attacker-controlled `from`:**

```rust
self.protocol
    .forward_rate_limiter
    .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
``` [2](#0-1) 

The limiter is configured at 1 request per second per `(from, to, item_id)` tuple. Because `from` is attacker-controlled, an attacker can rotate through arbitrary fake peer IDs to generate a fresh bucket for every message, completely defeating the per-`(from,to)` forwarding cap. The only remaining guard is the outer `rate_limiter` keyed on `(session_id, msg_item_id)` at 30 req/s, which is the intended *per-session* cap, not the per-route forwarding cap. [3](#0-2) 

**2. `pending_delivered` map poisoned with attacker-chosen peer ID and listen addresses:**

When `self_peer_id == &content.to`, `respond_delivered` is called:

```rust
self.protocol
    .pending_delivered
    .insert(from_peer_id, (remote_listens, now));
``` [4](#0-3) 

`from_peer_id` comes from `content.from` (message body) and `remote_listens` comes from `content.listen_addrs` (also message body). An attacker can insert an arbitrary `(PeerId → Vec<Multiaddr>)` entry into the victim node's `pending_delivered` map, associating any peer ID with attacker-controlled addresses. This map is later consumed when a `ConnectionRequestDelivered` arrives to drive actual outbound TCP connection attempts for hole punching.

**Entry path:**

Any peer that has an open session with the victim node can send a `HolePunchingMessage::ConnectionRequest` with arbitrary `from`, `to`, and `listen_addrs` fields. No privilege is required beyond a normal P2P connection. [5](#0-4) 

### Impact Explanation

1. **Rate-limiter bypass / forwarding amplification**: The `forward_rate_limiter` is the mechanism that prevents a single peer from causing the victim to forward an unbounded number of `ConnectionRequest` messages to the rest of the network. By cycling through fake `from` values, an attacker saturates the victim's forwarding capacity up to the outer 30 req/s session cap, turning the victim into a relay amplifier. With multiple sessions this scales linearly.

2. **`pending_delivered` map poisoning**: The attacker can pre-populate the victim's `pending_delivered` map with entries mapping legitimate peer IDs to attacker-controlled addresses. When a genuine `ConnectionRequestDelivered` for that peer ID subsequently arrives, the victim node will attempt outbound TCP connections to the attacker's addresses instead of the real peer's addresses, disrupting hole-punching connectivity and potentially leaking connection metadata to the attacker.

### Likelihood Explanation

Any unprivileged peer that can establish a P2P connection to the victim node can trigger this. No keys, no special role, no majority hash power required. The Hole Punching protocol is enabled by default for full nodes. The attacker only needs to craft a valid `ConnectionRequest` molecule-encoded message with a spoofed `from` field, which is trivial.

### Recommendation

Bind the `from` field to the actual authenticated session peer ID. In `ConnectionRequestProcess::execute()`, after parsing `content`, assert:

```rust
let actual_peer_id = self
    .protocol
    .network_state
    .peer_registry
    .read()
    .get_peer(self.peer)
    .and_then(|p| extract_peer_id(&p.connected_addr));

if actual_peer_id.as_ref() != Some(&content.from) {
    return StatusCode::InvalidFromPeerId
        .with_context("from field does not match authenticated session peer id");
}
```

This mirrors the recommended fix in the original report: check the actual caller identity (`msg.sender == L2_CROSS_DOMAIN_MESSENGER`) rather than trusting data returned by or supplied by the caller.

### Proof of Concept

1. Attacker establishes a normal P2P session with victim node V.
2. Attacker sends a `HolePunchingMessage::ConnectionRequest` with:
   - `from` = arbitrary fake `PeerId` (e.g., random bytes forming a valid peer ID)
   - `to` = V's own peer ID (so `respond_delivered` is triggered)
   - `listen_addrs` = attacker-controlled IP:port
3. V's `execute()` parses `content.from` = fake peer ID, checks `forward_rate_limiter` against `(fake_id, V_id, item_id)` — passes (fresh bucket).
4. V calls `respond_delivered`, inserts `pending_delivered[fake_id] = (attacker_addrs, now)`.
5. Attacker repeats step 2 with a different `from` each time, bypassing the 1/s `forward_rate_limiter` cap and filling `pending_delivered` with poisoned entries.
6. For the forwarding amplification variant: set `to` = some other peer ID (not V), so `forward_message` is called instead, causing V to broadcast the request to `sqrt(total_peers)` neighbors per message, amplified by the number of fake `from` values the attacker cycles through. [6](#0-5)

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
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

**File:** network/src/protocols/hole_punching/mod.rs (L109-120)
```rust
        let status = match msg {
            packed::HolePunchingMessageUnionReader::ConnectionRequest(reader) => {
                component::ConnectionRequestProcess::new(
                    reader,
                    self,
                    context.session.id,
                    context.control(),
                    msg.item_id(),
                )
                .execute()
                .await
            }
```
