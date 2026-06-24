Audit Report

## Title
Unauthenticated Peer ID Spoofing Enables Arbitrary NAT Traversal via `pending_delivered` — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

## Summary

`ConnectionRequestProcess::respond_delivered` stores attacker-controlled TCP addresses in `pending_delivered` keyed by `content.from`, a field taken verbatim from the message payload with no validation against the authenticated session peer (`self.peer`). `ConnectionSyncProcess` carries no session peer field and performs no such validation either, so when it looks up `pending_delivered[content.from]` and spawns NAT traversal tasks, any connected peer can cause the victim node to open outbound TCP connections to arbitrary IP:port combinations. The `forward_rate_limiter` is trivially bypassed with fresh synthetic peer IDs, leaving only the 30 req/s per-session throttle.

## Finding Description

**Root cause — Step 1: Populate `pending_delivered` with attacker-controlled addresses.**

`ConnectionRequestProcess` stores the real session peer as `self.peer` (a `PeerIndex`), but `execute()` passes `content.from` — taken verbatim from the message payload — directly to `respond_delivered` without comparing it to `self.peer`:

```rust
// connection_request.rs L145-147
if self_peer_id == &content.to {
    self.respond_delivered(content.from, &content.to, content.listen_addrs)
        .await
``` [1](#0-0) 

Inside `respond_delivered`, after filtering to TCP/IPv4/IPv6 addresses (L196–215), the attacker-supplied addresses are stored under the attacker-chosen `from_peer_id`:

```rust
// connection_request.rs L234-237
let now = unix_time_as_millis();
self.protocol
    .pending_delivered
    .insert(from_peer_id, (remote_listens, now));
``` [2](#0-1) 

The `HOLE_PUNCHING_INTERVAL` guard (L161–167) only blocks re-insertion for the *same* `from_peer_id`; fresh synthetic IDs bypass it entirely. [3](#0-2) 

**Root cause — Step 2: Trigger NAT traversal without peer verification.**

`ConnectionSyncProcess` is constructed without a session peer argument: [4](#0-3) 

Its struct has no `peer` field: [5](#0-4) 

When `content.route` is empty and `content.to == local_peer_id`, it unconditionally looks up `pending_delivered[content.from]` and spawns traversal tasks with no check that `content.from` matches the actual session peer: [6](#0-5) 

A successful traversal is promoted to a full P2P session via `control.raw_session(stream, addr, RawSessionInfo::inbound(...))`: [7](#0-6) 

**Rate-limiter bypass.**

The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)` for both `ConnectionRequest` and `ConnectionSync`: [8](#0-7) [9](#0-8) 

Using a fresh synthetic `from` peer ID for each pair of messages produces a new key, bypassing this limiter entirely. The only remaining throttle is the per-session `rate_limiter` keyed by `(session_id, msg.item_id())` at 30 req/s: [10](#0-9) 

Since `ConnectionRequest` and `ConnectionSync` have different `item_id` values, the attacker can sustain up to 30 address pairs per second from a single session.

**Secondary impact — unbounded map growth.**

`pending_delivered` is only cleaned up in `notify()` every 5 minutes: [11](#0-10) 

At 30 insertions/second with up to 24 addresses each, the map can accumulate ~9,000 entries (each holding a `Vec<Multiaddr>`) before the first cleanup, contributing to memory pressure.

## Impact Explanation

**High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

A single connected peer can exhaust the victim's outbound connection slots by directing it to connect to attacker-controlled endpoints, crashing or severely degrading the node. The unbounded growth of `pending_delivered` adds a secondary memory exhaustion vector.

**High (10001–15000 points) — Bad designs which could cause CKB network congestion with few costs.**

At scale, an attacker operating multiple sessions across the network can eclipse victim nodes by filling their connection tables with attacker-controlled peers, isolating them from the honest network. The cost is a single authenticated session per victim.

## Likelihood Explanation

Any peer that can establish a single authenticated P2P session with the target node can execute this attack immediately. No special privileges, leaked keys, or majority hashpower are required. The two-message sequence (`ConnectionRequest` then `ConnectionSync`) is trivially constructable from the public protocol schema. The attacker needs only one session and can sustain the attack at 30 address pairs/second indefinitely.

## Recommendation

1. **In `ConnectionRequestProcess::respond_delivered`**: resolve `self.peer` (a `PeerIndex`) to a `PeerId` via the peer registry and reject the message if `content.from` does not match the resolved peer ID. The peer registry lookup pattern is already used in `forward_message` at L250–255 of the same file. [12](#0-11) 

2. **In `ConnectionSyncProcess`**: pass the session `PeerIndex` (as is done for `ConnectionRequestProcess` in `mod.rs` L114) and verify it resolves to the same `PeerId` as `content.from` before performing the `pending_delivered` lookup and spawning traversal tasks. [13](#0-12) 

3. **Re-key `forward_rate_limiter`** by `(session_id, msg_item_id)` rather than `(content.from, content.to, msg_item_id)` to prevent bypass via synthetic peer IDs. [14](#0-13) 

4. **Cap `pending_delivered` size** to prevent unbounded memory growth under sustained attack.

## Proof of Concept

```
Setup: Attacker controls session B connected to victim node V.

1. Attacker sends over session B:
   ConnectionRequest {
     from = <synthetic_id_X>,   // any valid PeerId bytes, not attacker's real ID
     to   = <local_peer_id_V>,
     listen_addrs = [/ip4/1.2.3.4/tcp/9999],
     route = [],
     max_hops = 1
   }
   → V: content.to == local_peer_id → respond_delivered() called
   → forward_rate_limiter key (X, V, ConnectionRequest_item_id) is fresh → passes
   → HOLE_PUNCHING_INTERVAL check: no prior entry for X → passes
   → TCP/IPv4 filter passes (/ip4/1.2.3.4/tcp/9999)
   → pending_delivered[X] = ([/ip4/1.2.3.4/tcp/9999], now)

2. Attacker sends over session B:
   ConnectionSync {
     from  = <synthetic_id_X>,
     to    = <local_peer_id_V>,
     route = []
   }
   → V: route is empty, content.to == local_peer_id
   → forward_rate_limiter key (X, V, ConnectionSync_item_id) is fresh → passes
   → pending_delivered.get(X) → Some([/ip4/1.2.3.4/tcp/9999])
   → try_nat_traversal(bind_addr, /ip4/1.2.3.4/tcp/9999) spawned
   → V opens TCP connection to 1.2.3.4:9999

3. Repeat steps 1–2 with fresh synthetic_id_X', X'', ... values.
   forward_rate_limiter sees new keys each time → no throttle.
   HOLE_PUNCHING_INTERVAL sees new keys each time → no throttle.
   Only per-session rate_limiter applies: up to 30 pairs/second.

4. To exhaust connection slots: target attacker-controlled peers at
   1.2.3.4:9999 … N; each successful raw_session() consumes an outbound slot.
   To eclipse: direct all slots to attacker-controlled peers.
   To port-scan: use arbitrary third-party IP:port combinations as targets.
```

### Citations

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L250-255)
```rust
        let target_sid = self
            .protocol
            .network_state
            .peer_registry
            .read()
            .get_key_by_peer_id(to_peer_id);
```

**File:** network/src/protocols/hole_punching/mod.rs (L46-46)
```rust
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
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

**File:** network/src/protocols/hole_punching/mod.rs (L110-120)
```rust
            packed::HolePunchingMessageUnionReader::ConnectionRequest(reader) => {
                component::ConnectionRequestProcess::new(
                    reader,
                    self,
                    context.session.id,
                    context.control(),
                    msg.item_id(),
                )
                .execute()
                .await
            }
```

**File:** network/src/protocols/hole_punching/mod.rs (L133-143)
```rust
            packed::HolePunchingMessageUnionReader::ConnectionSync(reader) => {
                component::ConnectionSyncProcess::new(
                    reader,
                    self,
                    context.control(),
                    self.bind_addr,
                    msg.item_id(),
                )
                .execute()
                .await
            }
```

**File:** network/src/protocols/hole_punching/mod.rs (L173-175)
```rust
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L51-57)
```rust
pub(crate) struct ConnectionSyncProcess<'a> {
    message: packed::ConnectionSyncReader<'a>,
    protocol: &'a HolePunching,
    p2p_control: &'a ServiceAsyncControl,
    bind_addr: Option<SocketAddr>,
    msg_item_id: u32,
}
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-124)
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
