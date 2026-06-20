### Title
Unauthenticated `from` Field in Hole-Punching Protocol Allows Attacker to Poison `pending_delivered` and Force NAT Traversal to Attacker-Controlled Addresses — (`network/src/protocols/hole_punching/component/connection_request.rs`, `connection_sync.rs`)

---

### Summary

An unprivileged peer can spoof the `from` field in both `ConnectionRequest` and `ConnectionSync` messages. Because neither handler verifies that the `from` peer ID in the message payload matches the actual session that sent the message, an attacker can inject attacker-controlled `Multiaddr` values into the victim's `pending_delivered` map under any legitimate peer's ID, then immediately trigger `try_nat_traversal` to those addresses, causing the victim to establish a raw TCP session to attacker infrastructure.

---

### Finding Description

The hole-punching protocol uses three message types: `ConnectionRequest`, `ConnectionRequestDelivered`, and `ConnectionSync`. The victim node maintains a `pending_delivered: HashMap<PeerId, (Vec<Multiaddr>, u64)>` map that stores the listen addresses of the originating peer, keyed by that peer's ID.

**Step 1 — Poison `pending_delivered`:**

When a `ConnectionRequest` arrives with `to == self_peer_id`, `ConnectionRequestProcess::execute` calls `respond_delivered`: [1](#0-0) 

Inside `respond_delivered`, after filtering the addresses, the victim unconditionally inserts the attacker-supplied `remote_listens` into `pending_delivered` keyed by the message's `from` field: [2](#0-1) 

There is **no check** that the actual P2P session (`self.peer`) corresponds to the peer ID claimed in `content.from`. An attacker connected as peer A can set `from=B_peer_id` and `listen_addrs=[attacker_ip:port]`, causing the victim to store `pending_delivered[B_peer_id] = ([attacker_ip:port], now)`.

**Step 2 — Trigger NAT traversal:**

The attacker immediately sends a `ConnectionSync` with `from=B_peer_id` and `to=victim_peer_id` (empty `route`). `ConnectionSyncProcess::execute` identifies the victim as the target and looks up the poisoned entry: [3](#0-2) 

It then spawns `try_nat_traversal` tasks for every address in the poisoned list: [4](#0-3) 

On success, the victim calls `control.raw_session(...)` establishing a raw inbound session to the attacker's address: [5](#0-4) 

Again, **no check** that the `from` field in `ConnectionSync` matches the actual sending session.

**Why existing guards do not prevent this:**

- The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)` — it limits to 1 request/second per `(from, to)` tuple, but the attacker only needs one successful injection per 2-minute `HOLE_PUNCHING_INTERVAL` window. [6](#0-5) 
- The `HOLE_PUNCHING_INTERVAL` check only prevents re-poisoning the same `from` key within 2 minutes; it does not prevent the attacker from immediately following up with a `ConnectionSync`. [7](#0-6) 
- The address filtering in `respond_delivered` only rejects non-TCP/non-IP addresses; attacker-controlled TCP/IPv4 or IPv6 addresses pass through. [8](#0-7) 
- The peer ID embedded in `listen_addrs` is checked against `content.from` (not the actual session), so the attacker can embed `B_peer_id` in the multiaddr or omit it (the code appends it automatically). [9](#0-8) 

---

### Impact Explanation

The victim establishes a raw TCP session to attacker-controlled infrastructure via `control.raw_session(..., RawSessionInfo::inbound(listen_addr))`, which then runs the Identify protocol. This allows the attacker to:

- **Eclipse attack**: Displace legitimate peer connections with connections to attacker nodes, controlling the victim's view of the network.
- **Traffic interception**: All block/transaction relay the victim sends to or receives from the eclipsed connection goes through attacker infrastructure.
- **Peer slot exhaustion**: Repeated injections across different spoofed `from` peer IDs can fill the victim's outbound connection slots with attacker nodes. [5](#0-4) 

---

### Likelihood Explanation

The attacker only needs a single connected P2P session to the victim — a standard, unprivileged peer connection. No special role, no leaked keys, no majority hashpower. The two-message sequence (`ConnectionRequest` then `ConnectionSync`) can be sent back-to-back. The rate limiter allows one injection per second per `(from, to)` pair, and the attacker can rotate spoofed `from` peer IDs to bypass even that.

---

### Recommendation

1. **Verify `from` against the actual session**: In `respond_delivered`, check that the peer session that sent the `ConnectionRequest` (`self.peer`) corresponds to the peer ID in `content.from` via the peer registry. Reject the message if they do not match.
2. **Verify `from` in `ConnectionSync`**: In `ConnectionSyncProcess::execute`, when the node is the final target, verify that the session delivering the `ConnectionSync` is consistent with the expected return path (e.g., the session that originally sent the `ConnectionRequest`).
3. **Bind `pending_delivered` to session**: Store the session ID alongside the addresses in `pending_delivered` and require that the `ConnectionSync` arrives on the same session (or a session whose peer ID matches `from`).

---

### Proof of Concept

```
Attacker (peer A, session S_A) is connected to Victim (V).
Legitimate peer B has known peer_id = B_id.

1. Attacker sends over session S_A:
   ConnectionRequest {
     from: B_id,
     to: V_id,
     listen_addrs: [/ip4/1.2.3.4/tcp/9999],  // attacker-controlled
     route: [],
     max_hops: 6,
   }

2. Victim's respond_delivered executes:
   - Filters /ip4/1.2.3.4/tcp/9999 → passes (TCP + IPv4)
   - pending_delivered.insert(B_id, ([/ip4/1.2.3.4/tcp/9999/p2p/B_id], now))
   - Sends ConnectionRequestDelivered back to S_A

3. Attacker immediately sends over session S_A:
   ConnectionSync {
     from: B_id,
     to: V_id,
     route: [],
   }

4. Victim's ConnectionSyncProcess::execute:
   - self_peer_id == content.to → victim is target
   - listens_info = pending_delivered.get(B_id) → [/ip4/1.2.3.4/tcp/9999/p2p/B_id]
   - Spawns try_nat_traversal(bind_addr, /ip4/1.2.3.4/tcp/9999/p2p/B_id)
   - On TCP connect success: control.raw_session(stream, addr, RawSessionInfo::inbound(...))

Result: Victim has established a raw session to 1.2.3.4:9999 (attacker infrastructure).
``` [10](#0-9) [11](#0-10)

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L47-54)
```rust
                        if let Some(peer_id) = extract_peer_id(&addr) {
                            if peer_id != from {
                                return Err(StatusCode::InvalidListenAddrLen
                                    .with_context("peer id in listen address is invalid"));
                            }
                        } else {
                            addr.push(Protocol::P2P(Cow::Borrowed(from.as_bytes())));
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L155-239)
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
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L100-162)
```rust
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
