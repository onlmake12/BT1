Audit Report

## Title
Unauthenticated `from` PeerId in `ConnectionRequest` Allows `pending_delivered` Poisoning with Attacker-Controlled Addresses — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary

The `respond_delivered` function in `ConnectionRequestProcess` inserts attacker-supplied `listen_addrs` into `pending_delivered` keyed by the message's `from` PeerId field, which is parsed purely from message bytes and never verified against the actual sending session's PeerId. Any connected peer can spoof an arbitrary `from` PeerId and inject attacker-controlled TCP addresses into the map. When a subsequent `ConnectionSync` arrives for that PeerId, the victim node performs NAT traversal to the attacker-controlled addresses, potentially establishing an unauthorized raw p2p session.

## Finding Description

**Root cause — unauthenticated `from` field:**

In `connection_request.rs` lines 36–38, `from` is deserialized from message bytes with no binding to the actual session:

```rust
let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
    StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
})?;
``` [1](#0-0) 

The `execute()` function at line 145 routes to `respond_delivered` solely based on `content.to == self_peer_id`, with no check that `content.from` matches the session's actual PeerId: [2](#0-1) 

**Insufficient guard in `respond_delivered`:**

Lines 161–167 only reject re-insertion when an existing entry is less than `HOLE_PUNCHING_INTERVAL` (2 minutes) old. After that window, the unconditional overwrite at lines 234–237 executes:

```rust
self.protocol
    .pending_delivered
    .insert(from_peer_id, (remote_listens, now));
``` [3](#0-2) [4](#0-3) 

The `remote_listens` inserted are the attacker-supplied `listen_addrs` from the message, filtered only to TCP/IPv4/IPv6 — a filter that does not prevent attacker-controlled addresses from passing. [5](#0-4) 

**Consumption of the poisoned entry:**

In `connection_sync.rs` lines 111–115, `pending_delivered` is looked up by `content.from` (also unauthenticated, parsed from message bytes at line 42–44):

```rust
let listens_info = self
    .protocol
    .pending_delivered
    .get(&content.from)
    .map(|info| info.0.clone());
``` [6](#0-5) 

These addresses are passed to `try_nat_traversal`, which retries outbound TCP connections for up to 30 seconds: [7](#0-6) 

On success, `raw_session` is called with the resulting stream: [8](#0-7) 

**Full exploit flow:**

1. Attacker (connected peer E) sends `ConnectionRequest { from: A, to: V, listen_addrs: [attacker_addr] }` directly to V.
2. V's `execute()` sees `content.to == self_peer_id`, calls `respond_delivered(A, V, [attacker_addr])`.
3. Guard passes (no prior entry, or prior entry is >2 min old). V inserts `pending_delivered[A] = ([attacker_addr], now)`.
4. Attacker sends `ConnectionSync { from: A, to: V }` to V (also unauthenticated `from`).
5. V looks up `pending_delivered[A]`, gets `[attacker_addr]`, spawns `try_nat_traversal` to `attacker_addr` for 30 seconds.
6. Attacker's node accepts the TCP connection; `raw_session` establishes a p2p session with the attacker's node.

**Why existing guards fail:**

- The `rate_limiter` (30 req/sec per `(session_id, msg_item_id)`) does not prevent the attack — one message per 2 minutes suffices.
- The `forward_rate_limiter` (1 req/sec per `(from, to, msg_item_id)`) is trivially bypassed by waiting 1 second between the `ConnectionRequest` and `ConnectionSync`, or by using different `from` PeerIds to poison multiple entries simultaneously.
- The `HOLE_PUNCHING_INTERVAL` guard only prevents rapid re-poisoning of the same entry; it does not prevent initial poisoning or poisoning after the window expires. [9](#0-8) [10](#0-9) 

## Impact Explanation

The concrete impacts are:

1. **Unauthorized p2p session establishment:** The attacker causes the victim node to initiate a TCP connection to an attacker-controlled address and, on success, calls `raw_session` — establishing a full p2p session outside the normal connection admission path.
2. **Hole-punch denial:** The legitimate `pending_delivered` entry for peer A is overwritten, causing the legitimate hole-punch to fail.
3. **Resource exhaustion vector:** By using many distinct spoofed `from` PeerIds (each bypassing the per-`(from,to)` rate limiter), the attacker can spawn many concurrent 30-second NAT traversal tasks, consuming TCP sockets and async task resources on the victim node.

The resource exhaustion vector — spawning unbounded concurrent NAT traversal tasks via spoofed `from` PeerIds — maps to the **High** impact class: *Vulnerabilities which could easily crash a CKB node*, as the victim's async runtime and socket resources can be exhausted by a single connected attacker sending distinct `(from, to)` pairs at the rate limiter's allowed rate.

## Likelihood Explanation

- The attacker requires only a single established p2p connection to the victim — no special privilege, no cryptographic material, no PoW.
- The victim's PeerId and connected peers' PeerIds are publicly exchanged during peer identification.
- The attack is repeatable: after 2 minutes, the same entry can be re-poisoned. With distinct `from` PeerIds, there is no cooldown at all.
- The `forward_rate_limiter` key `(from, to, msg_item_id)` means the attacker can use `N` distinct spoofed `from` PeerIds to create `N` poisoned entries and `N` concurrent NAT traversal tasks in a single second, limited only by the per-session `rate_limiter` of 30 req/sec.

## Recommendation

1. **Verify `from` against the session.** In `respond_delivered`, look up the actual PeerId of `self.peer` from the peer registry and reject the message if `content.from` does not match it. This eliminates spoofing entirely.
2. **Do not allow overwrite of an unconsumed entry.** If `pending_delivered` already contains an entry for `from_peer_id` (regardless of age), reject the new request until the entry is consumed or explicitly expired.
3. **Bound concurrent NAT traversal tasks.** Maintain a counter or semaphore on active `try_nat_traversal` tasks to prevent resource exhaustion from bulk spoofed requests.

## Proof of Concept

```rust
// 1. Attacker (session E) sends ConnectionRequest { from: A, to: V, listen_addrs: [attacker_addr] }
//    No prior entry for A exists → guard passes → pending_delivered[A] = ([attacker_addr], now)

// 2. Verify poisoning
let (addrs, _) = protocol.pending_delivered.get(&peer_a).unwrap();
assert_eq!(addrs[0], attacker_addr);

// 3. Attacker sends ConnectionSync { from: A, to: V }
//    V looks up pending_delivered[A] → gets [attacker_addr]
//    V spawns try_nat_traversal(bind_addr, attacker_addr) for 30 seconds

// 4. Attacker's node at attacker_addr accepts the TCP connection
//    raw_session(stream, attacker_addr, RawSessionInfo::inbound(listen_addr)) is called
//    → unauthorized p2p session established

// 5. Scale: repeat with peer_b, peer_c, ... peer_N (distinct from PeerIds)
//    → N concurrent 30-second NAT traversal tasks, exhausting V's resources
```

The minimal unit test would construct a `HolePunching` protocol instance, call `ConnectionRequestProcess::new(...).execute()` with a spoofed `from`, assert the `pending_delivered` entry contains the attacker address, then call `ConnectionSyncProcess::new(...).execute()` and observe `try_nat_traversal` is invoked with the attacker address.

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L196-215)
```rust
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L154-160)
```rust
                                            let _ignore = control
                                                .raw_session(
                                                    stream,
                                                    addr,
                                                    RawSessionInfo::inbound(listen_addr),
                                                )
                                                .await;
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L65-68)
```rust
    let timeout_duration = Duration::from_secs(30);
    let start_time = Instant::now();
    let mut retry_count = 0u32;
    while start_time.elapsed() < timeout_duration {
```

**File:** network/src/protocols/hole_punching/mod.rs (L24-24)
```rust
pub(crate) const HOLE_PUNCHING_INTERVAL: u64 = 2 * 60 * 1000; // 2 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```
