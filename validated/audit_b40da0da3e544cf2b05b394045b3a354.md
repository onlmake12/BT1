Audit Report

## Title
Unauthenticated Peer Can Populate `pending_delivered` with Arbitrary TCP Addresses and Trigger NAT Traversal — (`network/src/protocols/hole_punching/component/connection_sync.rs`, `connection_request.rs`)

## Summary

`ConnectionRequestProcess::execute` inserts into `pending_delivered` keyed by `content.from`, a field taken verbatim from the message payload, without verifying it matches the actual authenticated session peer (`self.peer`). `ConnectionSyncProcess` carries no session peer field at all and unconditionally looks up `pending_delivered[content.from]`, then spawns outbound TCP connection attempts to the stored addresses. Any single connected peer can therefore direct the victim node to make TCP connections to arbitrary IP:port targets at up to 30 pairs per second.

## Finding Description

**Step 1 — Populate `pending_delivered` with attacker-controlled addresses.**

`ConnectionRequestProcess` holds the real session peer as `self.peer` (a `PeerIndex`), but `execute` passes `content.from` — taken directly from the wire message — to `respond_delivered` without comparing it to `self.peer`: [1](#0-0) 

Inside `respond_delivered`, the addresses are filtered to TCP/IPv4/IPv6 only (non-TCP transports are dropped), then inserted into `pending_delivered` keyed by the attacker-supplied `from_peer_id`: [2](#0-1) 

The 2-minute deduplication guard at lines 161–167 only blocks re-use of the *same* `from_peer_id`; a fresh synthetic ID bypasses it entirely.

**Step 2 — Trigger NAT traversal from the same session.**

`ConnectionSyncProcess` has no `peer` field — the session identity is structurally absent: [3](#0-2) 

When `content.to == local_peer_id` and `content.route` is empty, `execute` looks up `pending_delivered[content.from]` and immediately spawns `try_nat_traversal` tasks to every stored address: [4](#0-3) 

A successful TCP connection is promoted to a full P2P session via `raw_session`: [5](#0-4) 

**Why existing guards fail.**

The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)`: [6](#0-5) 

Using a fresh synthetic `from` peer ID per pair of messages bypasses this limiter entirely. The only remaining throttle is the per-session `rate_limiter` keyed by `(session_id, msg.item_id())`, capped at 30 req/s: [7](#0-6) 

## Impact Explanation

A single connected peer can cause the victim node to make outbound TCP connections to arbitrary IP:port combinations at up to 30 per second. Successful connections are promoted to full P2P sessions. This enables:

- **Connection slot exhaustion**: filling the outbound connection table with adversarial or useless sessions, degrading or crashing the node's ability to maintain legitimate peers — matching **High: Vulnerabilities which could easily crash a CKB node**.
- **Eclipse attack setup**: directing the victim to connect exclusively to attacker-controlled peers, isolating it from the honest network — matching **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.
- **Unsolicited TCP port scanning**: using the victim as a probe against arbitrary third-party hosts.

## Likelihood Explanation

Any peer that can establish a single P2P session with the target can execute this attack immediately. No leaked keys, special privileges, or majority hashpower are required. The two-message sequence (`ConnectionRequest` then `ConnectionSync`) is trivially constructable from the public protocol schema. The per-session 30 req/s rate limit is the only practical throttle, and it is generous enough to exhaust a typical node's outbound connection table within seconds.

## Recommendation

In `ConnectionRequestProcess::respond_delivered`, resolve `self.peer` (a `PeerIndex`) to a `PeerId` via the peer registry and reject the message if it does not equal `content.from`:

```rust
let actual_peer_id = self.protocol.network_state
    .with_peer_registry(|reg| reg.get_peer(self.peer).map(|p| p.peer_id.clone()));
if actual_peer_id.as_ref() != Some(&from_peer_id) {
    return StatusCode::InvalidFromPeerId.with_context("from does not match session peer");
}
```

In `ConnectionSyncProcess`, add a `peer: PeerIndex` field (mirroring `ConnectionRequestProcess`) and perform the same validation before the `pending_delivered` lookup.

## Proof of Concept

```
1. Attacker (session B, real peer ID = B) sends:
   ConnectionRequest {
       from = <synthetic_id_X>,   // not B
       to   = <local_node_id>,
       listen_addrs = [/ip4/1.2.3.4/tcp/9999],
       route = [], max_hops = 1
   }
   → respond_delivered: no existing entry for X, TCP filter passes,
     pending_delivered[X] = ([/ip4/1.2.3.4/tcp/9999/p2p/X], now)

2. Attacker (same session B) sends:
   ConnectionSync {
       from  = <synthetic_id_X>,
       to    = <local_node_id>,
       route = []
   }
   → execute(): route is empty, self_peer_id == content.to,
     listens_info = pending_delivered[X] = [/ip4/1.2.3.4/tcp/9999/...]
     → try_nat_traversal(bind_addr, /ip4/1.2.3.4/tcp/9999/...)
     → local node opens TCP connection to 1.2.3.4:9999
     → on success: raw_session() promotes it to a full P2P session

3. Repeat with fresh synthetic_id_X' values to bypass forward_rate_limiter,
   up to 30 times/second per the per-session rate_limiter.
   After ~N iterations (where N = max_outbound), all outbound slots are
   occupied by attacker-controlled sessions → node is eclipsed or exhausted.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
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
