Audit Report

## Title
Unauthenticated `from` Field in `ConnectionRequestDelivered` Enables Inflight-Request Drain and Attacker-Directed NAT Traversal — (`network/src/protocols/hole_punching/component/connection_request_delivered.rs`)

## Summary

The `from` field in `ConnectionRequestDelivered` is parsed directly from wire bytes with no verification that it matches the actual sender's session peer ID. Any connected peer can spoof `from` to equal the victim's own local peer ID and set `to` to a known `inflight_requests` key, causing the victim to drain its own inflight entry and then spawn up to 24 concurrent 30-second TCP connection loops to attacker-supplied addresses. The attack requires only a standard P2P connection and knowledge of a peer ID in `inflight_requests`, which is observable from gossip.

## Finding Description

**Root cause — unauthenticated `from` field:**

`DeliverdContent::try_from` parses `from` directly from the wire message:

```rust
let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
    StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
})?;
``` [1](#0-0) 

`self.peer` (the actual session `PeerIndex`) is stored in the struct but is only used inside `respond_sync` to echo a message back to the real sender; it is never resolved to a `PeerId` and never compared against `content.from`. [2](#0-1) 

**Reachable sensitive branch:**

`execute()` enters the terminal `None` branch when `content.route` is empty, then checks `self_peer_id == &content.from`. Because `content.from` is attacker-controlled, the attacker sets it to the victim's own local peer ID to satisfy this check:

```rust
None => {
    let self_peer_id = self.protocol.network_state.local_peer_id();
    if self_peer_id != &content.from {
        self.forward_delivered(&content.from).await
    } else {
        let request_start = self.protocol.inflight_requests.remove(&content.to);
        ...
        self.try_nat_traversal(ttl, content.listen_addrs);
    }
}
``` [3](#0-2) 

**Inflight-request drain:**

`inflight_requests` is a `HashMap<PeerId, u64>` populated by `notify()` every `CHECK_INTERVAL` (5 minutes). The attacker sets `content.to` to any key present in this map; `remove` is unconditional and returns `Some(start)`, which proceeds to `try_nat_traversal`. [4](#0-3) 

**Attacker-directed TCP connection loop:**

`try_nat_traversal` in `component/mod.rs` loops for 30 seconds at ~200 ms intervals, issuing a new TCP `connect()` on each iteration to the attacker-supplied address. With `ADDRS_COUNT_LIMIT = 24` addresses, 24 such tasks are spawned concurrently via `select_ok`, yielding up to ~3,600 outbound TCP SYN packets per attack invocation. [5](#0-4) 

On any successful connection, `control.raw_session()` is called, establishing a full P2P session to the attacker-controlled endpoint: [6](#0-5) 

**Rate-limiter bypass:**

The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)`. Since the attacker controls both `from` and `to`, they can use a distinct `to` value per `inflight_requests` entry to bypass the per-key limit for each drain attempt. [7](#0-6) 

**`listen_addrs` validation insufficient:**

The only address validation checks that any embedded peer ID matches `content.to`, which the attacker also controls, so arbitrary IP:port targets pass validation. [8](#0-7) 

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

With a single standard P2P connection, an attacker can:
1. Permanently suppress the victim's hole-punching capability for up to 5 minutes per cycle by draining all `inflight_requests` entries, degrading CKB network connectivity for NAT-traversal-dependent peers.
2. Amplify outbound TCP SYN traffic: 24 addresses × ~150 attempts = ~3,600 SYN packets per invocation, repeatable immediately. This constitutes low-cost traffic amplification usable for port-scan or connection-exhaustion attacks against third-party hosts, with the victim as the apparent source.
3. Establish an unsolicited raw P2P session to an attacker-controlled endpoint via `raw_session()`, bypassing normal peer-selection and potentially injecting protocol traffic.

## Likelihood Explanation

The attacker requires only a standard P2P connection to the victim — no special privileges. The victim's `inflight_requests` keys (`to` peer IDs) are observable directly from `ConnectionRequest` gossip, which is broadcast to a square-root subset of connected peers: [9](#0-8) 

The victim's own local peer ID (`from` value to spoof) is publicly known. The attack is fully deterministic, requires no brute-force, and is repeatable every 5 minutes as `notify()` repopulates `inflight_requests`.

## Recommendation

In `execute()`, before entering the `inflight_requests.remove` branch, resolve the actual sender's `PeerId` from `self.peer` via the peer registry and assert it equals `content.from`. If they differ, return a ban-worthy status. This mirrors the pattern already used in `forward_message` in `connection_request.rs`, where `self.peer` is the authoritative session identity. [10](#0-9) 

## Proof of Concept

```
Pre-condition:
  victim.inflight_requests = { peer_B_id: T }   // populated by notify()
  attacker is connected to victim as peer A

Step 1 — attacker observes peer_B_id from ConnectionRequest gossip broadcast.

Step 2 — attacker sends to victim:
  ConnectionRequestDelivered {
    from:         victim_local_peer_id,   // spoofed; publicly known
    to:           peer_B_id,              // observed from gossip
    route:        [],                     // empty → triggers None branch
    listen_addrs: [/ip4/1.2.3.4/tcp/9999],  // attacker-controlled
    sync_route:   [],
  }

Step 3 — victim execute():
  content.route.last() == None            → None branch
  self_peer_id == content.from            → else branch (line 154)
  inflight_requests.remove(peer_B_id)     → Some(T), entry drained
  try_nat_traversal(ttl, [1.2.3.4:9999]) → spawns 30-second TCP connect loop

Assertions:
  victim.inflight_requests.contains_key(peer_B_id) == false
  TCP SYN packets observed at 1.2.3.4:9999 for ~30 seconds
  victim's hole-punching to peer_B suppressed until next notify() (~5 min)

Repeat with distinct to values to drain all inflight entries; rate limiter
does not block because each (from, to, item_id) tuple is distinct.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L38-40)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L56-70)
```rust
                    Ok(mut addr) => {
                        if let Some(peer_id) = extract_peer_id(&addr) {
                            if peer_id != to {
                                return Err(StatusCode::InvalidListenAddrLen
                                    .with_context("peer id in listen address is invalid"));
                            }
                        } else {
                            addr.push(Protocol::P2P(Cow::Borrowed(to.as_bytes())));
                        }
                        Ok(addr)
                    }
                    Err(_) => Err(StatusCode::InvalidListenAddrLen
                        .with_context("the listen address is invalid")),
                },
            )
```

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

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L147-179)
```rust
        match content.route.last() {
            Some(next_peer_id) => self.forward_delivered(next_peer_id).await,
            None => {
                let self_peer_id = self.protocol.network_state.local_peer_id();
                if self_peer_id != &content.from {
                    // forward the message to the `from` peer
                    self.forward_delivered(&content.from).await
                } else {
                    // the current peer is the target peer, respond the sync back
                    if let Some(metrics) = ckb_metrics::handle() {
                        metrics.ckb_hole_punching_active_count.inc();
                    }

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
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L226-229)
```rust
        if let Err(error) = self
            .p2p_control
            .send_message_to(self.peer, proto_id, new_message)
            .await
```

**File:** network/src/protocols/hole_punching/component/connection_request_delivered.rs (L274-283)
```rust
                let _ignore = control
                    .raw_session(
                        stream,
                        addr,
                        RawSessionInfo::outbound(TargetProtocol::Single(
                            SupportProtocols::Identify.protocol_id(),
                        )),
                    )
                    .await;
            }
```

**File:** network/src/protocols/hole_punching/mod.rs (L223-235)
```rust
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
```

**File:** network/src/protocols/hole_punching/mod.rs (L239-242)
```rust
            let now = unix_time_as_millis();
            for peer_id in inflight {
                self.inflight_requests.insert(peer_id, now);
            }
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L62-111)
```rust
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
```
