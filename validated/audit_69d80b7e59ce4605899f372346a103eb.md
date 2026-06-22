### Title
Hole-Punching `from` Peer ID Spoofed from Attacker-Controlled Payload Bypasses Forward Rate Limiter and Enables SSRF — (`File: network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

In CKB's hole-punching protocol, the `from` peer ID in `ConnectionRequest`, `ConnectionRequestDelivered`, and `ConnectionSync` messages is extracted from the attacker-controlled message payload rather than from the actual transport-layer connection identity. This is a direct analog to the Auro wallet origin-spoofing bug: a security-relevant identity field is trusted from the message body instead of from the verified connection context. Any unprivileged connected peer can spoof the `from` field to (1) bypass the `forward_rate_limiter` entirely, enabling unlimited message amplification across the relay network, and (2) poison the `pending_delivered` table with attacker-chosen IP addresses, causing the victim node to initiate outbound TCP connections to arbitrary hosts (SSRF).

---

### Finding Description

**Root cause — `from` extracted from payload, not from connection:**

In `ConnectionRequestProcess::execute()`, the `from` peer ID is parsed directly from the message body:

```rust
// connection_request.rs, TryFrom impl
let from = PeerId::from_bytes(value.from().raw_data().to_vec())...
```

The actual `PeerIndex` of the sending connection (`self.peer`) is available and is used for the per-connection `rate_limiter`, but it is **never compared against `content.from`**. There is no assertion that `content.from` equals the peer ID of the session that delivered the message.

**Impact 1 — Forward rate-limiter bypass:**

The `forward_rate_limiter` is keyed by the tuple `(content.from, content.to, msg_item_id)`:

```rust
self.protocol
    .forward_rate_limiter
    .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
    .is_err()
```

Because `content.from` is fully attacker-controlled, an attacker can rotate a fresh random `from` value in every message, producing a unique key each time and making the rate limiter completely ineffective. The same bypass applies identically in `ConnectionRequestDeliveredProcess` and `ConnectionSyncProcess`, which use the same keying scheme.

**Impact 2 — `pending_delivered` poisoning / SSRF:**

When the victim node is the `to` target of a `ConnectionRequest`, it calls `respond_delivered`, which stores the message's `listen_addrs` (also attacker-controlled) under the spoofed `from` key:

```rust
self.protocol
    .pending_delivered
    .insert(from_peer_id, (remote_listens, now));
```

Later, when a `ConnectionSync` arrives with `from` set to the same spoofed peer ID, the node retrieves those stored addresses and initiates outbound TCP connections to them via `try_nat_traversal`:

```rust
let listens_info = self.protocol.pending_delivered
    .get(&content.from)
    .map(|info| info.0.clone());
// ...
let tasks = listens.into_iter()
    .map(|listen_addr| Box::pin(try_nat_traversal(self.bind_addr, listen_addr)))
    .collect::<Vec<_>>();
```

An attacker connected to the victim node can therefore cause it to make outbound TCP connections to any IP address by:
1. Sending `ConnectionRequest` with `from=<any_peer_id>`, `to=<victim_peer_id>`, `listen_addrs=[<attacker_chosen_IPs>]`
2. Sending `ConnectionSync` with `from=<same_peer_id>`, `to=<victim_peer_id>`

---

### Impact Explanation

- **Rate-limiter bypass / amplification**: The `forward_rate_limiter` is the only throttle on message forwarding. With `from` spoofing, a single connected attacker can generate unlimited forwarded `ConnectionRequest` / `ConnectionRequestDelivered` / `ConnectionSync` messages, each with a fresh random `from`, causing every relay node to forward them without limit. This is a network-wide amplification attack reachable from a single peer connection.
- **SSRF**: The victim node initiates real TCP connections (with `SO_REUSEPORT` on Linux) to attacker-specified IP:port pairs. This can be used to port-scan internal networks, trigger services behind firewalls, or exhaust the node's connection resources.

---

### Likelihood Explanation

Any peer that can establish a single P2P connection to a CKB node can send `HolePunching` protocol messages. No special privilege, key, or majority hashpower is required. The hole-punching protocol is enabled by default in the network stack. The attack requires only crafting a valid `ConnectionRequest` molecule-encoded message with a spoofed `from` field, which is trivial.

---

### Recommendation

1. **Verify `from` against the actual connection identity**: In `ConnectionRequestProcess`, assert that `content.from` equals the peer ID of the session that sent the message (available via `context.session` → peer registry lookup by `self.peer`). Reject messages where they do not match.
2. **Key the `forward_rate_limiter` on the actual sending `PeerIndex`** (`self.peer`) rather than on the payload-supplied `content.from`. This mirrors the existing per-connection `rate_limiter` design.
3. **Validate `listen_addrs` against the actual session address** before storing them in `pending_delivered`, or discard `pending_delivered` entries that were not initiated by the node itself.

---

### Proof of Concept

```
Attacker (connected peer) → Victim node:

Step 1: Send HolePunchingMessage::ConnectionRequest {
    from: <random_peer_id_A>,   // spoofed, not attacker's real peer ID
    to:   <victim_peer_id>,     // victim's known peer ID
    listen_addrs: [/ip4/192.168.1.1/tcp/8080],  // attacker-chosen target
    max_hops: 6,
    route: [],
}
→ Victim calls respond_delivered(), stores pending_delivered[random_peer_id_A] = ([192.168.1.1:8080], now)

Step 2: Send HolePunchingMessage::ConnectionSync {
    from: <random_peer_id_A>,   // same spoofed ID
    to:   <victim_peer_id>,
    route: [],
}
→ Victim looks up pending_delivered[random_peer_id_A], finds [192.168.1.1:8080],
  spawns try_nat_traversal() → outbound TCP SYN to 192.168.1.1:8080

For rate-limiter bypass: repeat Step 1 with a fresh random_peer_id_B, _C, _D, ...
→ Each message passes forward_rate_limiter with a unique key, no throttling applied.
```

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L132-143)
```rust
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
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-125)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());

                    match listens_info {
                        Some(listens) => {
                            let tasks = listens
                                .into_iter()
                                .map(|listen_addr| {
                                    Box::pin(try_nat_traversal(self.bind_addr, listen_addr))
                                })
                                .collect::<Vec<_>>();

```

**File:** network/src/protocols/hole_punching/mod.rs (L45-46)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```
