Audit Report

## Title
Unauthenticated Peer ID Spoofing Enables Arbitrary NAT Traversal via `pending_delivered` — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

## Summary

`ConnectionRequestProcess::respond_delivered` inserts attacker-controlled TCP addresses into `pending_delivered` keyed by `content.from`, a field taken verbatim from the message payload with no validation against the authenticated session peer (`self.peer`). `ConnectionSyncProcess` carries no session peer field and performs no such validation either, so when it looks up `pending_delivered[content.from]` and spawns NAT traversal tasks, there is no mechanism to verify the sender is the claimed peer. A single connected peer can cause the local node to make outbound TCP connection attempts to arbitrary IP:port combinations, with the `forward_rate_limiter` trivially bypassed using fresh synthetic peer IDs.

## Finding Description

**Step 1 — Populate `pending_delivered` with attacker-controlled addresses.**

`ConnectionRequestProcess` carries the real session peer as `self.peer` (a `PeerIndex`) at [1](#0-0) , but `execute()` passes `content.from` — taken verbatim from the message payload — directly to `respond_delivered` without comparing it to `self.peer`: [2](#0-1) 

Inside `respond_delivered`, after filtering to TCP/IPv4/IPv6 addresses only (lines 196–215), the attacker-supplied addresses are stored keyed by `from_peer_id` which is `content.from` — any syntactically valid peer ID the attacker chooses: [3](#0-2) 

The `HOLE_PUNCHING_INTERVAL` guard only blocks re-insertion for the *same* `from_peer_id` within 2 minutes; fresh synthetic IDs bypass it entirely: [4](#0-3) 

**Step 2 — Trigger NAT traversal from the same session.**

`ConnectionSyncProcess` is constructed without a session peer argument: [5](#0-4) 

Its struct has no `peer` field: [6](#0-5) 

When `content.route` is empty and `content.to == local_peer_id`, it unconditionally looks up `pending_delivered[content.from]` and spawns traversal tasks with no check that `content.from` matches the actual session peer: [7](#0-6) 

A successful traversal is promoted to a full P2P session: [8](#0-7) 

**Rate-limiter analysis.**

The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)`: [9](#0-8) 

Using a fresh synthetic `from` peer ID for each pair of messages produces a new key, bypassing this limiter entirely. The only remaining throttle is the per-session `rate_limiter` keyed by `(session_id, msg.item_id())` at 30 req/s: [10](#0-9) 

Each spawned `try_nat_traversal` task retries TCP connections for up to 30 seconds: [11](#0-10) 

At 30 pairs/second, after 30 seconds there are up to 900 concurrent traversal tasks running. Additionally, `pending_delivered` grows at 30 entries/second and is only cleaned up every 5 minutes, accumulating up to 9,000 entries per session.

## Impact Explanation

A single connected peer can cause the victim node to make outbound TCP connection attempts to arbitrary IP:port combinations at up to 30 address pairs per second. Successful connections are promoted to full P2P sessions via `raw_session`. This concretely enables:

- **Connection slot exhaustion / node crash**: 900 concurrent `try_nat_traversal` tasks each making repeated TCP connection attempts, combined with successful `raw_session` promotions filling the outbound connection table. This matches **High — Vulnerabilities which could easily crash a CKB node**.
- **Eclipse attack**: directing the victim to connect exclusively to attacker-controlled peers, isolating it from the honest network. This matches **High — bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation

Any peer that can establish a single authenticated P2P session with the target node can execute this attack immediately. No special privileges, leaked keys, or majority hashpower are required. The two-message sequence (`ConnectionRequest` then `ConnectionSync`) is trivially constructable using the public protocol schema. The attacker needs only one session and can sustain the attack at 30 address pairs/second indefinitely, with the `forward_rate_limiter` providing no meaningful protection due to the synthetic peer ID bypass.

## Recommendation

In `ConnectionRequestProcess::respond_delivered`, resolve `self.peer` (a `PeerIndex`) to a `PeerId` via the peer registry and reject the message if `content.from` does not match the resolved peer ID. The peer registry lookup is already used elsewhere in the same file (e.g., `forward_message` at lines 250–255).

In `ConnectionSyncProcess`, pass the session `PeerIndex` (as is done for `ConnectionRequestProcess` in `mod.rs` line 114) and verify it resolves to the same `PeerId` as `content.from` before performing the `pending_delivered` lookup and spawning traversal tasks.

Additionally, key `forward_rate_limiter` by `(session_id, msg_item_id)` rather than `(content.from, content.to, msg_item_id)` to prevent bypass via synthetic peer IDs.

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
   → forward_rate_limiter key (X, V, 1) is fresh → passes
   → TCP/IPv4 filter passes (/ip4/1.2.3.4/tcp/9999)
   → pending_delivered[X] = ([/ip4/1.2.3.4/tcp/9999], now)

2. Attacker sends over session B:
   ConnectionSync {
     from  = <synthetic_id_X>,
     to    = <local_peer_id_V>,
     route = []
   }
   → V: route is empty, content.to == local_peer_id
   → pending_delivered.get(X) → Some([/ip4/1.2.3.4/tcp/9999])
   → try_nat_traversal(bind_addr, /ip4/1.2.3.4/tcp/9999) spawned
   → V opens TCP connection to 1.2.3.4:9999

3. Repeat steps 1–2 with fresh synthetic_id_X' values.
   forward_rate_limiter sees new keys each time → no throttle.
   Only per-session rate_limiter applies: up to 30 pairs/second.

4. After 30 seconds: ~900 concurrent try_nat_traversal tasks running.
   Successful raw_session() calls consume outbound connection slots.
   To exhaust slots: target attacker-controlled peers at 1.2.3.4:9999…N.
```

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

**File:** network/src/protocols/hole_punching/component/mod.rs (L65-66)
```rust
    let timeout_duration = Duration::from_secs(30);
    let start_time = Instant::now();
```
