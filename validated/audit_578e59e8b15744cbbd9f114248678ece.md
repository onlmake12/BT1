The code is fully readable. Let me analyze the exact exploit path.

**Key facts from the code:**

1. `ConnectionRequest.from` is an unauthenticated field — it is just raw bytes parsed as a `PeerId` with no check that it matches the actual sending session's peer ID. [1](#0-0) 

2. The `respond_delivered` deduplication check keys on `from_peer_id` taken directly from the message content: [2](#0-1) 

3. On success, the victim inserts `pending_delivered[from_peer_id] = (remote_listens, now)` — keyed by the attacker-supplied `from` value: [3](#0-2) 

4. `HOLE_PUNCHING_INTERVAL` is 2 minutes: [4](#0-3) 

5. There is no check anywhere in `execute()` or `respond_delivered()` that `content.from` equals the actual session's peer ID (`self.peer`): [5](#0-4) 

The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)` — it limits to 1 req/sec for the same `(from, to)` pair, but the attacker only needs **one** spoofed request to poison the cache for 2 minutes. [6](#0-5) 

---

### Title
Unauthenticated `from` Field in `ConnectionRequest` Allows Cache Poisoning to Suppress Legitimate Hole-Punching Responses for 2 Minutes — (`network/src/protocols/hole_punching/component/connection_request.rs`)

### Summary
Any connected peer can send a `ConnectionRequest` with an arbitrary `from` field set to a victim's target peer ID. The victim node's `respond_delivered` function uses this unauthenticated `from` value as the key into `pending_delivered`, poisoning the deduplication cache and causing all legitimate hole-punching requests from the spoofed peer to be silently dropped for `HOLE_PUNCHING_INTERVAL` (2 minutes).

### Finding Description
In `ConnectionRequestProcess::execute()`, when the local node is the `to` target, it calls `respond_delivered(content.from, ...)` where `content.from` is taken directly from the message without verifying it matches the actual sending session's peer ID.

Inside `respond_delivered`:
```rust
if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
    let now = unix_time_as_millis();
    if now - t < HOLE_PUNCHING_INTERVAL {
        return StatusCode::Ignore
            .with_context("a same message is already replied in a moment ago");
    }
}
```
On first receipt, the check passes and the victim inserts `pending_delivered[from_peer_id] = (remote_listens, now)`. Any subsequent legitimate `ConnectionRequest` with the same `from` peer ID arriving within 2 minutes is silently dropped with `StatusCode::Ignore`.

The `TryFrom` implementation for `RequestContent` only validates that `from` is a syntactically valid `PeerId` and that embedded peer IDs in `listen_addrs` match `from` — it never checks that `from` equals the actual session's authenticated peer ID. [7](#0-6) 

### Impact Explanation
- An attacker with a single P2P connection to the victim node can deny hole-punching service for any `(from, to)` pair where `to` is the victim, for 2 minutes per spoofed request.
- The attack can be renewed every 2 minutes to maintain a persistent DoS.
- This prevents NAT traversal connections from being established, degrading network connectivity. Nodes behind NAT that rely on hole-punching to reach the victim will fail to connect, reducing the victim's peer diversity and potentially causing it to miss blocks or transactions, which is an indirect path to consensus deviation.

### Likelihood Explanation
The attack requires only a single P2P connection to the victim (any peer can connect to a CKB node). The attacker needs to know the `PeerId` of the legitimate peer they want to block — peer IDs are publicly discoverable via the discovery protocol. The attack is trivially repeatable and requires no special privileges.

### Recommendation
Validate that `content.from` matches the authenticated peer ID of the actual sending session. In `execute()`, look up the session's peer ID from `self.protocol.network_state.peer_registry` using `self.peer` and reject the message if `content.from != actual_sender_peer_id`. This ensures the `from` field cannot be spoofed.

### Proof of Concept
```
1. Attacker connects to victim node (victim peer ID = V).
2. Attacker knows legitimate peer ID L (discoverable via discovery protocol).
3. Attacker sends ConnectionRequest{from=L, to=V, listen_addrs=[<valid TCP addr>], max_hops=6, route=[]}.
4. Victim: self_peer_id == &content.to → calls respond_delivered(L, V, [...]).
5. pending_delivered.get(&L) → None → proceeds.
6. Victim sends ConnectionRequestDelivered back to attacker's session.
7. Victim inserts pending_delivered[L] = ([...], now).
8. Within 2 minutes, legitimate peer L sends ConnectionRequest{from=L, to=V, ...}.
9. Victim: calls respond_delivered(L, V, [...]).
10. pending_delivered.get(&L) → Some((_, t)) where now - t < HOLE_PUNCHING_INTERVAL.
11. Returns StatusCode::Ignore → legitimate request silently dropped.
12. Attacker repeats step 3 every ~2 minutes to maintain the DoS indefinitely.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L32-83)
```rust
impl TryFrom<&packed::ConnectionRequestReader<'_>> for RequestContent {
    type Error = Status;

    fn try_from(value: &packed::ConnectionRequestReader<'_>) -> Result<Self, Self::Error> {
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
        let to = PeerId::from_bytes(value.to().raw_data().to_vec())
            .map_err(|_| StatusCode::InvalidToPeerId.with_context("the to peer id is invalid"))?;
        let listen_addrs: Vec<Multiaddr> = value
            .listen_addrs()
            .iter()
            .map(
                |raw| match Multiaddr::try_from(raw.bytes().raw_data().to_vec()) {
                    Ok(mut addr) => {
                        if let Some(peer_id) = extract_peer_id(&addr) {
                            if peer_id != from {
                                return Err(StatusCode::InvalidListenAddrLen
                                    .with_context("peer id in listen address is invalid"));
                            }
                        } else {
                            addr.push(Protocol::P2P(Cow::Borrowed(from.as_bytes())));
                        }
                        Ok(addr)
                    }
                    Err(_) => Err(StatusCode::InvalidListenAddrLen
                        .with_context("the listen address is invalid")),
                },
            )
            .collect::<Result<Vec<_>, _>>()?;

        let route: Vec<PeerId> = value
            .route()
            .iter()
            .map(|raw| {
                PeerId::from_bytes(raw.raw_data().to_vec()).map_err(|_| {
                    StatusCode::InvalidRoute.with_context("the route peer id is invalid")
                })
            })
            .collect::<Result<Vec<_>, _>>()?;

        let max_hops: u8 = value.max_hops().into();

        Ok(Self {
            from,
            to,
            listen_addrs,
            route,
            max_hops,
        })
    }
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

**File:** network/src/protocols/hole_punching/mod.rs (L24-24)
```rust
pub(crate) const HOLE_PUNCHING_INTERVAL: u64 = 2 * 60 * 1000; // 2 minutes
```
