The code confirms a real, exploitable vulnerability. Let me trace the exact path.

**Key observation**: In `respond_delivered`, the `from_peer_id` used as the `pending_delivered` map key comes entirely from the message payload — it is never checked against the actual session's authenticated peer ID.

### Title
Unauthenticated `from` Field in `ConnectionRequest` Allows `pending_delivered` Poisoning via PeerId Spoofing — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

`respond_delivered` in `ConnectionRequestProcess` inserts attacker-controlled `listen_addrs` into `pending_delivered` keyed by the message-supplied `from` PeerId, without ever verifying that `from` matches the actual authenticated session peer. Any connected peer can spoof an arbitrary `from` PeerId and overwrite the victim node's `pending_delivered` entry for that peer, redirecting passive NAT traversal to attacker-controlled addresses.

---

### Finding Description

The `ConnectionRequest` message schema carries a `from` field (raw bytes) that is fully attacker-controlled: [1](#0-0) 

When the receiving node is the intended `to` target, `execute()` calls `respond_delivered(content.from, ...)`: [2](#0-1) 

Inside `respond_delivered`, the only guard is a timestamp deduplication check against `HOLE_PUNCHING_INTERVAL` (2 minutes): [3](#0-2) 

If no entry exists for the spoofed `from_peer_id`, or the existing entry is older than 2 minutes, the function proceeds to insert attacker-controlled `remote_listens` into `pending_delivered`: [4](#0-3) 

**The missing check**: `ConnectionRequestProcess` holds `self.peer: PeerIndex` (the actual authenticated session ID), but it is never used to verify that `content.from` matches the real peer identity of the sender. The `from` field is accepted verbatim from the message payload. [5](#0-4) 

The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)` — all attacker-controlled values — and allows 1 request/second per `(from, to)` pair. A single request is sufficient to poison the entry; the rate limiter provides no meaningful protection. [6](#0-5) 

---

### Impact Explanation

`pending_delivered` is consumed in `ConnectionSyncProcess` when a `ConnectionSync` arrives with `from = spoofed_peer_id`: [7](#0-6) 

The addresses retrieved from `pending_delivered` are used directly for NAT traversal: [8](#0-7) 

By poisoning `pending_delivered[A]` with attacker-controlled TCP addresses, the victim node's passive hole-punch attempt for peer A is redirected to the attacker's endpoint. The victim initiates a raw TCP session to the attacker instead of the legitimate peer A, enabling connection hijacking of the NAT traversal path.

---

### Likelihood Explanation

- **Precondition**: Attacker must hold any valid P2P connection to the victim node — standard, unprivileged, no special role required.
- **PeerId of target peer A**: Public information, observable from the P2P network (peer store, discovery protocol, etc.).
- **Victim node's own PeerId**: Known from the connection handshake.
- **`listen_addrs` constraint**: Must be TCP with IPv4/IPv6 — trivially satisfiable with any attacker-controlled IP:port.
- **HOLE_PUNCHING_INTERVAL bypass**: If no prior entry exists (common for peers not recently seen), the attack succeeds immediately. If an entry exists, the attacker waits 2 minutes.
- **Rate limiter bypass**: The attacker sends exactly one message; the 1 req/sec limit is irrelevant.

The attack is fully local-testable, requires no hashpower, no leaked keys, and no privileged access.

---

### Recommendation

In `respond_delivered`, resolve the actual `PeerId` of the sending session from `self.peer` (the `PeerIndex`) via the peer registry, and reject the message if `content.from` does not match the authenticated session peer ID:

```rust
// In respond_delivered, before inserting into pending_delivered:
let actual_peer_id = self.protocol.network_state
    .peer_registry.read()
    .get_peer(self.peer)
    .map(|p| p.peer_id.clone());

match actual_peer_id {
    Some(id) if id == from_peer_id => { /* proceed */ }
    _ => return StatusCode::InvalidFromPeerId
             .with_context("from does not match session peer"),
}
```

This ensures `pending_delivered` entries are only populated from the peer whose identity is cryptographically authenticated by the transport layer.

---

### Proof of Concept

```
1. Victim node B has peer_id = B_id (known from connection).
2. Legitimate peer A has peer_id = A_id (known from discovery/peer store).
3. Attacker connects to B as any peer (standard P2P handshake).
4. Attacker sends ConnectionRequest {
       from: A_id,          // spoofed
       to:   B_id,          // victim's own ID
       listen_addrs: [/ip4/attacker_ip/tcp/attacker_port],
       max_hops: 6,
       route: [],
   }
5. B receives it; self_peer_id == content.to → calls respond_delivered(A_id, ...).
6. No existing pending_delivered[A_id] entry → interval check passes.
7. B inserts pending_delivered[A_id] = ([/ip4/attacker_ip/tcp/attacker_port], now).
8. When a ConnectionSync {from: A_id, to: B_id} later arrives (legitimate or
   attacker-crafted), B reads pending_delivered[A_id] and initiates NAT traversal
   to attacker_ip:attacker_port instead of A's real address.
```

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
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

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-115)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L119-123)
```rust
                            let tasks = listens
                                .into_iter()
                                .map(|listen_addr| {
                                    Box::pin(try_nat_traversal(self.bind_addr, listen_addr))
                                })
```
