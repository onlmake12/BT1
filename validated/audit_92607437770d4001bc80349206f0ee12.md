The code is clear enough to make a definitive determination. Let me trace the full attack path precisely.

**Key observations from the code:**

**1. No authentication of `from` field against actual sender session**

In `respond_delivered` (lines 155-237), the `from_peer_id` comes entirely from the message payload (`content.from`), not from the actual P2P session identity. There is zero check that `content.from` matches the peer ID of `self.peer` (the actual connected session). [1](#0-0) 

**2. The interval check only guards against re-insertion within 2 minutes — not against overwrite after expiry**

If `pending_delivered[L]` either doesn't exist or was inserted more than `HOLE_PUNCHING_INTERVAL` ago, the check passes and the attacker-controlled `remote_listens` are unconditionally written. [2](#0-1) [3](#0-2) 

**3. `ConnectionSync` consumes `pending_delivered` without any further validation**

When a `ConnectionSync` arrives with `from=L`, V blindly reads `pending_delivered[L]` and spawns TCP connection tasks to those addresses. [4](#0-3) 

**4. Rate limiter is not a meaningful defense**

The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)`. The attacker only needs to send one request after the 2-minute window, so the 1-req/sec limit per tuple is irrelevant. [5](#0-4) 

---

### Title
Unauthenticated `from` field in `ConnectionRequest` allows any peer to poison `pending_delivered` NAT traversal state — (`network/src/protocols/hole_punching/component/connection_request.rs`)

### Summary
`ConnectionRequestProcess::respond_delivered` inserts attacker-supplied `listen_addrs` into `pending_delivered` keyed by an arbitrary `from` peer ID taken directly from the message payload, with no verification that `from` matches the actual sending session. Any connected peer can overwrite the NAT traversal address cache for any peer ID, causing victim node V to initiate outbound TCP connections to attacker-controlled IPs when a `ConnectionSync` is subsequently processed.

### Finding Description
`respond_delivered` receives `from_peer_id` from `content.from` — a field the sender writes freely — and uses it as the key for `pending_delivered.insert(from_peer_id, (remote_listens, now))`. The only guard is a 2-minute deduplication window: if an entry for that key already exists and is less than `HOLE_PUNCHING_INTERVAL` old, the message is ignored. Once the window expires (or if no prior entry exists), the insert proceeds unconditionally with the attacker's `listen_addrs`.

The `ConnectionSyncProcess` later reads `pending_delivered.get(&content.from)` and passes those addresses directly to `try_nat_traversal`, which opens real TCP connections. [6](#0-5) 

### Impact Explanation
An attacker who is a connected peer to V can:
1. Send `ConnectionRequest { from: L_peer_id, to: V_peer_id, listen_addrs: [attacker_ip] }` — poisons `pending_delivered[L]` with attacker-controlled addresses.
2. Either wait for a legitimate `ConnectionSync { from: L, to: V }` to arrive, or immediately send a spoofed one (the `ConnectionSync` handler has the same absence of sender-identity verification).
3. V spawns TCP connection tasks to `attacker_ip`, and if successful, establishes a raw P2P session with the attacker's node under the identity of L.

This enables targeted eclipse-attack components: V's outbound NAT traversal slots are consumed connecting to the attacker instead of legitimate peers, and the attacker gains an inbound session to V that V believes was initiated toward L. [7](#0-6) 

### Likelihood Explanation
The attacker only needs a single active P2P session with V (standard unprivileged peer). No PoW, no key material, no privileged role. The 2-minute cooldown is trivially bypassed by waiting. The `forward_rate_limiter` (1 req/sec per `(from, to, item_id)` tuple) does not prevent a single poisoning write. The attack is locally testable and requires no network-wide resources.

### Recommendation
In `respond_delivered`, verify that `from_peer_id` matches the actual peer ID of the sending session (`self.peer`). Concretely, resolve `self.peer` (a `PeerIndex`) to its `PeerId` via the peer registry and reject the message if `content.from != actual_sender_peer_id`. The same check should be applied in `ConnectionSyncProcess` for the `from` field. [8](#0-7) 

### Proof of Concept
```
1. Attacker A connects to victim V as a normal peer (session S_A).
2. A sends: ConnectionRequest { from=L_peer_id, to=V_peer_id, max_hops=1, route=[], listen_addrs=[attacker_ip:port] }
3. V::respond_delivered runs:
   - pending_delivered.get(L_peer_id) → None (or expired) → check passes
   - Sends ConnectionRequestDelivered back to S_A (attacker receives V's addresses as bonus)
   - pending_delivered.insert(L_peer_id, ([attacker_ip:port], now))
4. A sends: ConnectionSync { from=L_peer_id, to=V_peer_id, route=[] }
5. V::ConnectionSyncProcess::execute runs:
   - content.to == self_peer_id → enters passive branch
   - pending_delivered.get(L_peer_id) → Some([attacker_ip:port])
   - Spawns try_nat_traversal tasks to attacker_ip:port
   - On TCP connect success: control.raw_session(stream, attacker_ip, ...) → V now has a live session to attacker
``` [9](#0-8) [10](#0-9)

### Citations

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L155-237)
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
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L98-162)
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

                            debug!(
                                "current peer is the target peer {}, start NAT traversal",
                                content.to
                            );

                            match self
                                .protocol
                                .network_state
                                .config
                                .listen_addresses
                                .first()
                                .cloned()
                            {
                                Some(listen_addr) => {
                                    let control: ServiceAsyncControl = self.p2p_control.clone();
                                    runtime::spawn(async move {
                                        if let Ok(((stream, addr), _)) = select_ok(tasks).await {
                                            debug!("NAT traversal success, addr: {:?}", addr);
                                            if let Some(metrics) = ckb_metrics::handle() {
                                                metrics
                                                    .ckb_hole_punching_passive_success_count
                                                    .inc();
                                            }

                                            let _ignore = control
                                                .raw_session(
                                                    stream,
                                                    addr,
                                                    RawSessionInfo::inbound(listen_addr),
                                                )
                                                .await;
                                        }
                                    });
```

**File:** network/src/protocols/hole_punching/mod.rs (L24-30)
```rust
pub(crate) const HOLE_PUNCHING_INTERVAL: u64 = 2 * 60 * 1000; // 2 minutes
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes

type PendingDeliveredInfo = (Vec<Multiaddr>, u64);
```
