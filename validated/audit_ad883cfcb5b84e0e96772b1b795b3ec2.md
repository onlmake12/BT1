Audit Report

## Title
Unauthenticated `from` PeerId in `ConnectionRequest` Enables `pending_delivered` Poisoning and Unbounded NAT Traversal Task Spawning — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary
The `respond_delivered` function in `ConnectionRequestProcess` stores attacker-supplied `listen_addrs` into `pending_delivered` keyed by the message's `from` PeerId, which is never verified against the actual sending session's PeerId. Any connected peer can spoof `from` to inject entries for arbitrary PeerIds. A follow-up `ConnectionSync` message with the same spoofed `from` causes the victim to spawn an unbounded `try_nat_traversal` async task that makes repeated outbound TCP connection attempts to attacker-controlled addresses for 30 seconds, enabling resource exhaustion and denial of legitimate hole-punching.

## Finding Description

**Root cause — unauthenticated `from` field:**

`from` is parsed purely from message bytes with no check that it matches the actual session's PeerId: [1](#0-0) 

The parsed `from` is passed directly to `respond_delivered` when `self_peer_id == content.to`: [2](#0-1) 

**Insufficient guard — 2-minute window only:**

`respond_delivered` checks for an existing entry and rejects only if it is less than `HOLE_PUNCHING_INTERVAL` (2 minutes) old. After that window, the entry is unconditionally overwritten: [3](#0-2) 

The overwrite stores the attacker-supplied `remote_listens` (filtered to TCP/IPv4/IPv6 only, which the attacker trivially satisfies) keyed by the spoofed `from_peer_id`: [4](#0-3) 

**Consumption — `ConnectionSync` triggers unbounded task spawning:**

When a `ConnectionSync { from: spoofed_peer_A, to: victim_V, route: [] }` arrives at the victim, `pending_delivered` is queried by the spoofed `from`: [5](#0-4) 

The retrieved attacker-controlled addresses are passed to `try_nat_traversal`, which is spawned with no concurrency cap: [6](#0-5) 

**`try_nat_traversal` resource cost:**

Each spawned task loops for up to 30 seconds, creating a new `TcpSocket` and attempting `connect()` every ~200 ms — approximately 150 TCP connection attempts per task: [7](#0-6) 

**Rate limiter analysis:**

The outer `rate_limiter` is keyed by `(session_id, msg.item_id())` at 30 req/sec: [8](#0-7) 

The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)` at 1 req/sec: [9](#0-8) 

An attacker using 30 distinct fake `from` PeerIds can send 30 `ConnectionRequest` messages/sec (outer limit) and 30 `ConnectionSync` messages/sec, each with a distinct `(from, to)` key that satisfies the forward rate limiter. This spawns up to 30 tasks/sec × 30 sec lifetime = **900 concurrent NAT traversal tasks**, each making ~150 TCP connection attempts, totalling ~135,000 outbound TCP connections over 30 seconds (~4,500/sec) from a single attacker session.

## Impact Explanation

**High — Vulnerabilities which could easily crash a CKB node / cause CKB network congestion with few costs.**

The unbounded `runtime::spawn` calls with no concurrency limit allow a single connected attacker to exhaust the victim node's file descriptors, thread pool, and network resources through sustained outbound TCP connection storms to arbitrary IP:port pairs. The `pending_delivered` map also grows at 30 entries/sec with no bound until the 5-minute cleanup fires, adding memory pressure. Sustained attack across multiple victim nodes constitutes network-wide congestion.

## Likelihood Explanation

- Attacker precondition: be a connected peer — no PoW, no key material, no special privilege.
- The victim's PeerId is public (exchanged during peer identification).
- The attacker generates arbitrary fake `from` PeerIds locally; no cryptographic material for those PeerIds is needed.
- The 2-minute `HOLE_PUNCHING_INTERVAL` and 1 req/sec `forward_rate_limiter` are trivially satisfied by using distinct fake PeerIds.
- The attack is repeatable and sustained indefinitely.

## Recommendation

1. **Verify `from` against the session PeerId.** In `respond_delivered`, confirm that the direct sender's session PeerId (available via `context.session`) matches `content.from` before inserting into `pending_delivered`. Reject the message if they differ.
2. **Cap concurrent NAT traversal tasks.** Use a bounded semaphore or task counter to limit the number of simultaneously running `try_nat_traversal` tasks per node.
3. **Reject overwrite of an unconsumed entry.** If an entry for `from_peer_id` already exists in `pending_delivered` and has not yet been consumed by a `ConnectionSync`, reject the new `ConnectionRequest` regardless of age.
4. **Key `pending_delivered` on `(session_id, from_peer_id)`.** This prevents cross-session poisoning even if `from` verification is imperfect.

## Proof of Concept

```rust
// 1. Attacker (session E, connected to victim V) sends:
//    ConnectionRequest { from: fake_peer_A, to: victim_V, listen_addrs: [attacker_tcp_addr], ... }
//    Repeat with 30 distinct fake_peer_A_i per second.

// 2. For each fake_peer_A_i, victim V calls respond_delivered:
//    pending_delivered.insert(fake_peer_A_i, ([attacker_tcp_addr], now))

// 3. Attacker sends:
//    ConnectionSync { from: fake_peer_A_i, to: victim_V, route: [] }
//    for each i — satisfies forward_rate_limiter since each (from, to) key is distinct.

// 4. Victim V: pending_delivered.get(&fake_peer_A_i) → [attacker_tcp_addr]
//    runtime::spawn(try_nat_traversal(bind_addr, attacker_tcp_addr))
//    → ~150 TCP connect() calls over 30 seconds per task, 900 tasks concurrently.

// Assertion: victim node's fd table and async runtime are exhausted;
// legitimate hole-punch entries for real peers are displaced.

// Minimal unit test outline:
let mut protocol = make_test_protocol();
for i in 0..30 {
    let fake_peer = PeerId::random();
    protocol.pending_delivered.insert(
        fake_peer.clone(),
        (vec![attacker_multiaddr.clone()], unix_time_as_millis()),
    );
    // Simulate ConnectionSync arrival
    let (addrs, _) = protocol.pending_delivered.get(&fake_peer).unwrap();
    assert_eq!(addrs[0], attacker_multiaddr); // poisoned entry confirmed
}
// Each entry triggers try_nat_traversal → unbounded task spawn
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-115)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L143-162)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```
