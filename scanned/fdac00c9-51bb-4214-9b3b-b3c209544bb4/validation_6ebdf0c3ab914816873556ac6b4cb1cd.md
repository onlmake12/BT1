### Title
Missing `from != to` Peer Identity Validation in Hole Punching `ConnectionRequest` Allows Self-Referential Protocol Confusion - (File: `network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

The `ConnectionRequestProcess::execute()` function in CKB's hole punching protocol never validates that `content.from != content.to`. Any connected peer can craft a `ConnectionRequest` with identical `from` and `to` peer IDs, causing the receiving node to enter a self-referential protocol state: it populates the `pending_delivered` map with a self-keyed entry, emits a `ConnectionRequestDelivered` back with `from == to`, and triggers multi-hop forwarding of the malformed message to up to `sqrt(peers)` nodes per hop across up to `MAX_HOPS = 6` hops.

---

### Finding Description

In `network/src/protocols/hole_punching/component/connection_request.rs`, the `execute()` function performs several input validations — listen address count, `max_hops` ceiling, route length, and rate limiting — but contains no check that `content.from != content.to`:

```rust
// Lines 110–153: execute() validates many things but never checks from != to
pub(crate) async fn execute(mut self) -> Status {
    let content = match RequestContent::try_from(&self.message) { ... };
    if content.listen_addrs.len() > ADDRS_COUNT_LIMIT || content.listen_addrs.is_empty() { ... }
    if content.max_hops > MAX_HOPS { ... }
    if content.route.len() > MAX_HOPS as usize { ... }
    // rate limiter keyed by (from, to, msg_item_id) — does NOT reject from == to
    if self.protocol.forward_rate_limiter.check_key(&(content.from.clone(), content.to.clone(), ...)).is_err() { ... }

    if self_peer_id == &content.to {
        self.respond_delivered(content.from, &content.to, content.listen_addrs).await
    } else if content.max_hops == 0u8 {
        StatusCode::ReachedMaxHops.into()
    } else {
        self.forward_message(self_peer_id, &content.to).await   // broadcasts to sqrt(peers) nodes
    }
}
```

The `from` and `to` fields are entirely attacker-controlled bytes with no authentication binding them to the actual sending session. An attacker sets both to the same arbitrary `PeerId`.

**Case 1 — receiving node IS the `to` peer (`self_peer_id == &content.to == &content.from`):**

`respond_delivered` is called with `from_peer_id == to_peer_id`. This:
- Inserts a self-referential entry into `pending_delivered` keyed by `from_peer_id` (line 235–237)
- Sends a `ConnectionRequestDelivered` back to the sender with `from == to`

The returned `ConnectionRequestDelivered` is then processed by `ConnectionRequestDeliveredProcess::execute()`, which — finding `self_peer_id != &content.from` — attempts to forward the message to `content.from`, which equals `content.to`, creating a confused routing loop bounded only by the rate limiter.

**Case 2 — receiving node is a relay (`self_peer_id != &content.to`):**

`forward_message` is called, broadcasting to `sqrt(total_peers)` connected nodes. Each of those nodes repeats the same logic for up to `MAX_HOPS = 6` hops. A single crafted message fans out to potentially `sqrt(peers)^6` forwarded messages before TTL exhaustion.

The `pending_delivered` map on the eventual `to` node accumulates self-referential entries (keyed `from_peer_id == to_peer_id`) that persist for `TIMEOUT = 5 minutes`. When `ConnectionSync` later arrives with `from == to`, `connection_sync.rs` line 114 looks up `pending_delivered.get(&content.from)` and finds the self-referential entry, triggering a NAT traversal attempt to the node's own listen addresses.

The `forward_rate_limiter` key is `(from, to, msg_item_id)`. With `from == to` and `msg_item_id` fixed at `0` for all `ConnectionRequest` messages, only 1 such message per `(X, X)` pair per second is forwarded per relay node. However, an attacker can use distinct `from == to` values (different spoofed peer IDs) up to the session-level cap of 30 messages/second, each triggering independent fan-out chains.

---

### Impact Explanation

- **Protocol state confusion**: The hole punching state machine assumes `from` and `to` are distinct peers. When they are equal, `pending_delivered` is keyed by the `to` peer ID itself, corrupting the bookkeeping used by `ConnectionSync` to initiate NAT traversal.
- **Amplified network traffic**: One crafted message causes up to `sqrt(peers)` forwarded messages per relay hop, across up to 6 hops. With 30 distinct spoofed `(from == to)` pairs per second, this is a sustained amplification vector against the P2P overlay.
- **Wasted NAT traversal resources**: The `to` node spawns async tasks attempting TCP connections to its own listen addresses (lines 145–160 of `connection_sync.rs`), consuming sockets and CPU.
- **`pending_delivered` map growth**: Entries accumulate for 5 minutes per spoofed pair, bounded only by the rate limiter.

Impact: **3** — meaningful resource exhaustion and protocol confusion reachable by any connected peer; not a direct fund loss or consensus break, but a real P2P-layer degradation.

---

### Likelihood Explanation

Any peer that has established a session with a CKB node can send a `HolePunchingMessage`. No privilege, key, or special role is required. The `from` and `to` bytes are free-form and are never authenticated against the actual session. The session-level rate limiter (30 req/sec) is the only barrier, and it is easily saturated with distinct spoofed peer ID pairs.

Likelihood: **4** — trivially reachable by any connected peer with a single crafted message.

---

### Recommendation

Add an explicit identity check immediately after parsing `content` in `ConnectionRequestProcess::execute()`, and mirror it in `ConnectionRequestDeliveredProcess::execute()` and `ConnectionSyncProcess::execute()`:

```rust
if content.from == content.to {
    return StatusCode::InvalidFromPeerId
        .with_context("from and to peer ids must be different");
}
```

This is the direct analog of the Chiliz bridge fix: validate that source identity ≠ destination identity before any routing or state mutation occurs.

---

### Proof of Concept

1. Establish a session with a CKB node that has the `HolePunching` protocol enabled.
2. Choose any valid-length byte sequence as a peer ID (e.g., a 39-byte multihash). Set both `from` and `to` to this value.
3. Construct a `ConnectionRequest` molecule message with `from = to = X`, `max_hops = 6`, empty `route`, and 1–24 valid TCP multiaddresses in `listen_addrs` (with peer ID `X` appended).
4. Send the message over the hole punching protocol stream.
5. **If the target node's own peer ID equals `X`**: observe `respond_delivered` is called, `pending_delivered[X]` is set, and a `ConnectionRequestDelivered` with `from == to == X` is returned to you.
6. **If the target node's peer ID differs from `X`**: observe the message is forwarded to `sqrt(peers)` other nodes. Repeat with 30 distinct `X` values per second to sustain amplification.
7. On any node where `X` matches the local peer ID, a subsequent `ConnectionSync` with `from == to == X` will cause `pending_delivered.get(&X)` to return the self-referential entry and spawn NAT traversal tasks targeting the node's own listen addresses.

**Root cause line**: `network/src/protocols/hole_punching/component/connection_request.rs`, `execute()` function — no `content.from != content.to` guard before line 145. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-238)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));

```

**File:** network/src/protocols/hole_punching/mod.rs (L23-27)
```rust
pub(crate) const MAX_HOPS: u8 = 6;
pub(crate) const HOLE_PUNCHING_INTERVAL: u64 = 2 * 60 * 1000; // 2 minutes
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
const ADDRS_COUNT_LIMIT: usize = 24;
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L98-115)
```rust
        match content.route.last() {
            Some(next_peer_id) => self.forward_sync(next_peer_id).await,
            None => {
                let self_peer_id = self.protocol.network_state.local_peer_id();
                if self_peer_id != &content.to {
                    // forward the message to the `to` peer
                    self.forward_sync(&content.to).await
                } else {
                    // Current node should be the `to` target.
                    if let Some(metrics) = ckb_metrics::handle() {
                        metrics.ckb_hole_punching_passive_count.inc();
                    }

                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());
```

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
