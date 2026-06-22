### Title
Missing `from` Peer ID Verification in Hole Punching `ConnectionRequest` Handler Allows Attacker to Poison `pending_delivered` State and Bypass Rate Limits - (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

The `ConnectionRequestProcess::execute()` handler in the hole punching protocol parses the `from` field directly from the attacker-controlled message without verifying it matches the actual sending peer (`self.peer`). This is the direct CKB analog of the flash loan initiator check vulnerability: a trusted intermediary (the P2P session layer) delivers the message, but the handler blindly trusts a self-reported identity field inside the message payload. Any connected peer can forge `content.from` to be any arbitrary peer ID, poisoning the `pending_delivered` map and bypassing the per-pair rate limiter.

---

### Finding Description

In `ConnectionRequestProcess::execute()`, the `from` field is decoded from the wire message:

```rust
let from = PeerId::from_bytes(value.from().raw_data().to_vec())...;
``` [1](#0-0) 

The actual sender is available as `self.peer` (a `PeerIndex`), but `content.from` is **never cross-checked** against it. The handler then uses the unverified `content.from` in two security-sensitive ways:

**1. Rate limiter keyed on forged identity:**

```rust
self.protocol.forward_rate_limiter
    .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
``` [2](#0-1) 

An attacker can rotate `content.from` across arbitrary peer IDs to bypass the per-`(from, to)` rate limit entirely.

**2. `pending_delivered` map poisoned with forged peer ID and attacker-controlled addresses:**

When the local node is the intended target (`self_peer_id == &content.to`), `respond_delivered` is called with the forged `from_peer_id`:

```rust
self.protocol.pending_delivered.insert(from_peer_id, (remote_listens, now));
``` [3](#0-2) 

The `pending_delivered` map is subsequently consulted to suppress duplicate responses:

```rust
if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
    let now = unix_time_as_millis();
    if now - t < HOLE_PUNCHING_INTERVAL {
        return StatusCode::Ignore.with_context("a same message is already replied in a moment ago");
    }
}
``` [4](#0-3) 

An attacker who forges `content.from = victim_peer_id` causes the node to record a fresh timestamp for the victim. Any subsequent legitimate `ConnectionRequest` from the real victim peer is silently dropped for the duration of `HOLE_PUNCHING_INTERVAL`.

The same missing check exists in `ConnectionRequestDeliveredProcess::execute()`, where `content.from` is also taken from the message without verification against `self.peer`: [5](#0-4) 

When `self_peer_id == &content.from` (the local node's public peer ID, which is advertised via the identify protocol), the handler removes an entry from `inflight_requests` and calls `try_nat_traversal` with attacker-supplied `listen_addrs`, causing the node to initiate outbound TCP connections to arbitrary addresses: [6](#0-5) 

---

### Impact Explanation

1. **Hole-punching DoS**: Any connected peer can permanently suppress hole-punching between the target node and any victim peer by forging `content.from = victim_peer_id` and refreshing the `pending_delivered` timestamp repeatedly. The victim's legitimate `ConnectionRequest` messages are silently ignored.

2. **Rate limiter bypass**: The per-`(from, to)` rate limiter is trivially bypassed by rotating the forged `from` value, enabling message flooding.

3. **Forced outbound connections (SSRF-like)**: Via the `ConnectionRequestDelivered` path, an attacker knowing the local node's peer ID (public) can trigger `try_nat_traversal` with attacker-controlled multiaddrs, causing the node to initiate TCP connections to arbitrary IP:port targets.

---

### Likelihood Explanation

The hole punching protocol (`SupportProtocols::HolePunching`) was introduced in v0.202.0 (June 2025) and is reachable by any unprivileged connected P2P peer. No special privileges, keys, or majority hash power are required. The local node's peer ID is publicly advertised via the identify protocol, making the `ConnectionRequestDelivered` attack path fully practical. [7](#0-6) 

---

### Recommendation

In `ConnectionRequestProcess::execute()`, after parsing `content.from`, verify it matches the actual sender by looking up `self.peer` in the peer registry:

```rust
// Verify the `from` field matches the actual sender
let actual_peer_id = self.protocol.network_state
    .peer_registry.read()
    .get_peer(self.peer)
    .map(|p| p.peer_id.clone());
if actual_peer_id.as_ref() != Some(&content.from) {
    return StatusCode::InvalidFromPeerId
        .with_context("from field does not match actual sender");
}
```

Apply the same check in `ConnectionRequestDeliveredProcess::execute()` for the `content.from` field.

---

### Proof of Concept

1. Attacker connects to target node (Node B) via the P2P network.
2. Attacker sends a `HolePunchingMessage::ConnectionRequest` with:
   - `from = victim_peer_id` (any known peer ID, e.g. obtained from the peer store or identify messages)
   - `to = node_B_peer_id` (the local peer ID of Node B, publicly known)
   - `listen_addrs = [attacker_controlled_addr]`
   - `max_hops = 1`, `route = []`
3. Node B's `ConnectionRequestProcess::execute()` sees `self_peer_id == &content.to`, calls `respond_delivered(victim_peer_id, ...)`, and inserts `(victim_peer_id, (attacker_addrs, now))` into `pending_delivered`.
4. The real victim peer subsequently sends a legitimate `ConnectionRequest` to Node B. Node B checks `pending_delivered.get(&victim_peer_id)`, finds a fresh timestamp, and returns `StatusCode::Ignore` — the hole punch is silently dropped.
5. Attacker repeats step 2 every `HOLE_PUNCHING_INTERVAL` milliseconds to maintain the DoS indefinitely. [8](#0-7)

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L35-38)
```rust
    fn try_from(value: &packed::ConnectionRequestReader<'_>) -> Result<Self, Self::Error> {
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L161-167)
```rust
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L38-42)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
            .map_err(|_| StatusCode::InvalidToPeerId.with_context("the to peer id is invalid"))?;
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L160-178)
```rust
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
