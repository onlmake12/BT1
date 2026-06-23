### Title
Unauthenticated Third-Party Trigger of NAT Traversal to Arbitrary TCP Addresses via `pending_delivered` — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

### Summary

`ConnectionSyncProcess::execute` looks up `pending_delivered` using `content.from` taken directly from the message payload, with no check that the sending session peer matches that `from` peer ID. Combined with the fact that `ConnectionRequest` also does not validate `content.from` against the actual session peer, any connected peer can (1) populate `pending_delivered[X]` with attacker-chosen TCP addresses, then (2) trigger NAT traversal to those addresses — all without being peer X.

### Finding Description

**Step 1 — Populate `pending_delivered` with attacker-controlled addresses.**

`ConnectionRequestProcess::execute` calls `respond_delivered(content.from, …, content.listen_addrs)` when `content.to == local_peer_id`. [1](#0-0) 

`respond_delivered` inserts into `pending_delivered` keyed by `from_peer_id`, which is `content.from` — a field taken verbatim from the message payload, never compared to `self.peer` (the actual authenticated session index). [2](#0-1) 

`ConnectionRequestProcess` does carry the real session peer as `self.peer`, but it is never used to validate `content.from`. [3](#0-2) 

**Step 2 — Trigger NAT traversal from a different session.**

`ConnectionSyncProcess` has **no `peer` field at all** — the session peer ID is structurally unavailable: [4](#0-3) 

When `content.to == local_peer_id`, `execute` looks up `pending_delivered[content.from]` and immediately spawns NAT traversal tasks to the stored addresses: [5](#0-4) 

There is no check that the peer sending this `ConnectionSync` is the same peer whose ID appears in `content.from`.

**Rate-limiter does not prevent the attack.**

The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)` where `msg_item_id` is the fixed union discriminant (2 for `ConnectionSync`): [6](#0-5) 

An attacker using a fresh synthetic `from` peer ID for each pair of messages bypasses this limiter entirely. The per-session `rate_limiter` (30 req/s) is the only remaining throttle. [7](#0-6) 

**Address filtering is TCP/IP only — still exploitable.**

`respond_delivered` filters `listen_addrs` to TCP addresses with IPv4/IPv6, so the attacker cannot use exotic transports, but any `host:port` reachable over TCP is a valid target. [8](#0-7) 

### Impact Explanation

A single connected peer can cause the local node to make outbound TCP connection attempts to arbitrary IP:port combinations at up to 30 pairs per second (per-session rate limit). Successful connections are promoted to full P2P sessions via `raw_session`. This enables:

- **Eclipse attack setup**: directing the victim node to connect to attacker-controlled peers.
- **Connection slot exhaustion**: filling the outbound connection table with useless or adversarial sessions.
- **Unsolicited port scanning**: using the victim node as a TCP probe against third-party hosts.

### Likelihood Explanation

Any peer that can establish a single P2P session with the target node can execute this attack immediately. No special privileges, leaked keys, or majority hashpower are required. The two-message sequence (`ConnectionRequest` then `ConnectionSync`) is trivially constructable.

### Recommendation

In `ConnectionRequestProcess::respond_delivered`, validate that `content.from` matches the actual session peer ID before inserting into `pending_delivered`. The session peer ID is already available as `self.peer`; resolve it to a `PeerId` via the peer registry and reject the message if it does not match `content.from`.

In `ConnectionSyncProcess`, pass the session peer ID (as done for `ConnectionRequestProcess`) and verify it matches `content.from` before performing the `pending_delivered` lookup.

### Proof of Concept

```
1. Attacker (session B) sends:
   ConnectionRequest { from = <synthetic_id_X>, to = <local_id>,
                       listen_addrs = [/ip4/1.2.3.4/tcp/9999] }
   → respond_delivered stores pending_delivered[X] = ([/ip4/1.2.3.4/tcp/9999], now)

2. Attacker (same session B) sends:
   ConnectionSync { from = <synthetic_id_X>, to = <local_id>, route = [] }
   → execute() finds pending_delivered[X]
   → spawns try_nat_traversal(bind_addr, /ip4/1.2.3.4/tcp/9999)
   → local node opens TCP connection to 1.2.3.4:9999

3. Repeat with fresh synthetic_id_X values to bypass the forward_rate_limiter,
   up to 30 times/second per the per-session rate_limiter.
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
