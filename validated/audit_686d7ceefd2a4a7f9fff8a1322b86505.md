Audit Report

## Title
Unauthenticated Route-Bypass Message Injection via Missing Route Membership Check in `ConnectionRequestDeliveredProcess::execute` — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

## Summary

The `execute()` method in `ConnectionRequestDeliveredProcess` unconditionally forwards a `ConnectionRequestDelivered` message to any peer named in the attacker-controlled `content.from` field when `route` is empty, without verifying that the relay ever participated in a legitimate hole-punching route for that `(from, to)` pair. Any unprivileged peer connected to a relay can exploit this to inject fully attacker-crafted messages to any other peer connected to the same relay. When the victim has an active `inflight_requests` entry, it is induced to spawn long-lived async tasks making repeated TCP connection attempts to attacker-controlled endpoints. The `forward_rate_limiter` is trivially bypassed by varying `content.to`, allowing sustained injection up to 30 messages per second per relay.

## Finding Description

**Root cause — missing route membership check (lines 147–153):**

In `execute()`, when `content.route` is empty, `route.last()` returns `None`. The `None` branch checks only whether `local_peer_id == content.from`; if not (always true when the attacker sets `from` to a victim peer ID), it calls `forward_delivered(&content.from)` with no verification that the relay ever forwarded a `ConnectionRequest` for this `(from, to)` pair:

```rust
match content.route.last() {
    Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
    None => {
        let self_peer_id = self.protocol.network_state.local_peer_id();
        if self_peer_id != &content.from {
            self.forward_delivered(&content.from).await  // ← no route membership check
``` [1](#0-0) 

**Unconditional forwarding (lines 182–212):**

`forward_delivered` performs a `peer_registry` read-lock lookup for the attacker-supplied peer ID and, if connected, sends the full attacker-crafted message to it. No state is consulted to confirm the relay's prior participation in a legitimate route. [2](#0-1) 

**Rate limiter bypass (lines 134–145):**

The `forward_rate_limiter` is keyed on `(content.from, content.to, msg_item_id)`. All three values are attacker-controlled. By varying `content.to` across requests, the attacker generates distinct keys and bypasses the 1-req/sec-per-key limit. The only remaining cap is the session-level `rate_limiter` at 30 req/sec per `(session_id, item_id)`, confirmed in `mod.rs`. [3](#0-2) [4](#0-3) 

**Victim-side resource exhaustion (lines 160–176 and `mod.rs` lines 49–115):**

When the victim receives the injected message (`route = []`, `from = victim_peer_id`), it enters the "target peer" branch and calls `inflight_requests.remove(&content.to)`. If a matching entry exists (populated routinely by the `notify` timer when the node has fewer outbound connections than `max_outbound`), it spawns `try_nat_traversal(ttl, content.listen_addrs)` — a 30-second loop making TCP connection attempts (~150 total at ~200ms intervals) to fully attacker-controlled IP:port endpoints. [5](#0-4) [6](#0-5) [7](#0-6) 

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

The relay-side injection is unconditional: any unprivileged peer connected to a relay can cause that relay to forward up to 30 crafted `ConnectionRequestDelivered` messages per second to any other peer connected to the same relay, with zero legitimate routing state required. By connecting to multiple relay nodes simultaneously, an attacker multiplies this rate linearly (30 × N relays). Each injected message consumes processing resources on the victim and, when `inflight_requests` entries exist, spawns long-lived async tasks that open TCP sockets to attacker-chosen endpoints — amplifying resource exhaustion on the victim node. The combination of unauthenticated relay-side forwarding, a bypassable rate limiter, and victim-side resource consumption constitutes a low-cost, scalable bad design that can cause CKB P2P network congestion and victim node resource exhaustion.

## Likelihood Explanation

**Relay-side injection:** Requires only that the attacker be a connected P2P peer (any unprivileged node) and know the peer ID of a victim also connected to the same relay. Peer IDs are publicly observable on the CKB P2P network. No special privileges, leaked keys, or victim mistakes are required.

**Victim-side resource exhaustion escalation:** Requires additionally that the victim has an active `inflight_requests` entry for `content.to`. This is populated automatically whenever the victim's `notify` timer fires (every 5 minutes per `CHECK_INTERVAL`) and the victim has fewer outbound connections than `max_outbound` — a routine background condition for any under-connected node. [8](#0-7) 

The attack is repeatable: after `inflight_requests` entries are consumed via `remove`, the victim repopulates them at the next `notify` tick, restoring the resource exhaustion surface.

## Recommendation

In the `None` (empty route) branch of `execute()`, before calling `forward_delivered(&content.from)`, verify that the local node was a legitimate relay for this `(from, to)` pair. Concretely:

1. Maintain a bounded set (e.g., `HashSet<(PeerId, PeerId)>`) of `(from, to)` pairs for which the node has previously forwarded a `ConnectionRequest` (populated in `ConnectionRequestProcess::execute` when `forward_message` is called).
2. In the `None` branch of `ConnectionRequestDeliveredProcess::execute`, reject (`StatusCode::Ignore`) any message whose `(content.from, content.to)` pair is not present in this set.
3. Expire entries from the set after a reasonable TTL (e.g., `TIMEOUT` = 5 minutes) to bound memory usage.

Additionally, key the `forward_rate_limiter` on `(sender_session_id, content.from)` rather than `(content.from, content.to, item_id)` to prevent bypass via varying `content.to`.

## Proof of Concept

**Minimal manual steps:**

1. Attacker peer A connects to relay node R via the CKB P2P protocol.
2. Victim peer V is also connected to R (V's peer ID is observable from the P2P network).
3. A sends a `ConnectionRequestDelivered` message to R with:
   - `route = []` (empty)
   - `from = V.peer_id`
   - `to = <any valid PeerId, varied per request to bypass rate limiter>`
   - `listen_addrs = [attacker-controlled IP:port]` (1–24 addresses)
   - `sync_route = []`
4. R evaluates `route.last() == None`, checks `local_peer_id != V.peer_id` → calls `forward_delivered(V.peer_id)`.
5. R finds V in `peer_registry` and sends the crafted message to V.
6. V evaluates `route.last() == None`, checks `local_peer_id == V.peer_id` → enters "target peer" branch.
7. V calls `inflight_requests.remove(&content.to)`. If an entry exists, V calls `try_nat_traversal(ttl, [attacker-controlled IP:port])`, making ~150 TCP connection attempts over 30 seconds to the attacker's chosen endpoint.
8. Repeat step 3 with a new `content.to` value to bypass the `forward_rate_limiter` and sustain injection at up to 30 msg/sec.

**Invariant/fuzz test plan:**

- Property: for any `ConnectionRequestDelivered` message received by a relay, `forward_delivered` must only be called if the relay's `forwarded_requests` set contains `(content.from, content.to)`.
- Fuzz: generate random `ConnectionRequestDelivered` messages with `route = []` and `from` set to a peer ID present in `peer_registry` but absent from any legitimate route state; assert that `forward_delivered` is never invoked.

### Citations

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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L147-153)
```rust
        match content.route.last() {
            Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
            None => {
                let self_peer_id = self.protocol.network_state.local_peer_id();
                if self_peer_id != &content.from {
                    // forward the message to the `from` peer
                    self.forward_delivered(&content.from).await
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L160-176)
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
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L182-212)
```rust
    async fn forward_delivered(&self, peer_id: &PeerId) -> Status {
        let target_sid = self
            .protocol
            .network_state
            .peer_registry
            .read()
            .get_key_by_peer_id(peer_id);
        match target_sid {
            Some(next_peer) => {
                let content = forward_delivered(self.message);
                let new_message = packed::HolePunchingMessage::new_builder()
                    .set(content)
                    .build()
                    .as_bytes();
                let proto_id = SupportProtocols::HolePunching.protocol_id();
                debug!(
                    "forward the delivery to next peer {} (id: {})",
                    next_peer, peer_id
                );
                if let Err(error) = self
                    .p2p_control
                    .send_message_to(next_peer, proto_id, new_message)
                    .await
                {
                    StatusCode::ForwardError.with_context(error)
                } else {
                    Status::ok()
                }
            }
            None => StatusCode::Ignore.with_context("the next peer in the route is disconnected"),
        }
```

**File:** network/src/protocols/hole_punching/mod.rs (L25-28)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
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

**File:** network/src/protocols/hole_punching/mod.rs (L169-242)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
        let status = self.network_state.connection_status();

        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);

        if status.non_whitelist_outbound < status.max_outbound && status.total > 0 {
            let target = &self.network_state.required_flags;
            let addrs = self.network_state.with_peer_store_mut(|p| {
                p.fetch_nat_addrs(
                    (status.max_outbound - status.non_whitelist_outbound) as usize,
                    *target,
                )
            });

            let from_peer_id = self.network_state.local_peer_id();
            let listen_addrs = {
                let public_addr = self.network_state.public_addrs(ADDRS_COUNT_LIMIT);
                if public_addr.len() < ADDRS_COUNT_LIMIT {
                    let observed_addrs = self
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

            let mut inflight = Vec::new();
            for i in addrs {
                if let Some(to_peer_id) = extract_peer_id(&i.addr) {
                    let conn_req = {
                        let content = component::init_request(
                            from_peer_id,
                            &to_peer_id,
                            listen_addrs.clone(),
                        );
                        packed::HolePunchingMessage::new_builder()
                            .set(content)
                            .build()
                    };
                    let proto_id = SupportProtocols::HolePunching.protocol_id();

                    // Broadcast to a number of nodes equal to the square root of the total connection count using gossip.
                    let mut total = status.total.isqrt();
                    let _ignore = context
                        .filter_broadcast(
                            TargetSession::Filter(Box::new(move |_| {
                                total = total.saturating_sub(1);
                                total != 0
                            })),
                            proto_id,
                            conn_req.as_bytes(),
                        )
                        .await;
                    inflight.push(to_peer_id);
                }
            }

            let now = unix_time_as_millis();
            for peer_id in inflight {
                self.inflight_requests.insert(peer_id, now);
            }
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L49-115)
```rust
pub(crate) async fn try_nat_traversal(
    bind_addr: Option<SocketAddr>,
    addr: Multiaddr,
) -> Result<(TcpStream, Multiaddr), std::io::Error> {
    let net_addr = match multiaddr_to_socketaddr(&addr) {
        Some(addr) => addr,
        None => {
            debug!("Failed to convert multiaddr to socketaddr");
            return Err(std::io::ErrorKind::InvalidInput.into());
        }
    };

    // Use a fixed interval but add a small amount of randomness
    let base_retry_interval = Duration::from_millis(200);

    // total time
    let timeout_duration = Duration::from_secs(30);
    let start_time = Instant::now();
    let mut retry_count = 0u32;
    while start_time.elapsed() < timeout_duration {
        retry_count += 1;

        // Add a small amount of random jitter (±25ms) to avoid conflicts
        // caused by continuous precise synchronization
        let jitter = Duration::from_millis(rand::random::<u64>() % 50);
        let actual_interval = if rand::random::<bool>() {
            base_retry_interval + jitter
        } else {
            base_retry_interval.saturating_sub(jitter)
        };

        let socket = create_socket(bind_addr, net_addr)?;

        match runtime::timeout(
            std::time::Duration::from_millis(200),
            socket.connect(net_addr),
        )
        .await
        {
            Ok(Ok(stream)) => {
                // try get the stored error in the underlying socket
                // if the socket is not connected, it will return an error
                if let Err(err) = check_connection(&stream) {
                    debug!("Failed to connect to NAT(base check): {}", err);
                }
                return Ok((stream, addr));
            }
            Err(err) => {
                debug!("Failed to connect to NAT(timeout): {}", err);
            }
            Ok(Err(err)) => {
                if err.kind() == std::io::ErrorKind::AddrNotAvailable {
                    return Err(err);
                }
                debug!(
                    "Failed to connect to NAT(other error): {}, {}",
                    err.kind(),
                    err
                );
            }
        }
        runtime::delay_for(actual_interval).await;
    }

    debug!("Failed to connect to NAT after {} retries", retry_count);
    Err(std::io::ErrorKind::TimedOut.into())
}
```
