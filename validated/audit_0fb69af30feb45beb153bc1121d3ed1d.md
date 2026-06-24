Audit Report

## Title
Unauthenticated Resource Exhaustion via Hole-Punching Protocol: Rotating `from` Peer IDs Bypass Rate Limits, Exhausting File Descriptors and Memory — (`network/src/protocols/hole_punching/component/connection_request.rs`, `connection_sync.rs`)

## Summary
An unprivileged peer can cause a CKB node to spawn unbounded `try_nat_traversal` tasks — each holding a TCP socket for up to 30 seconds — by sending `ConnectionRequest` messages with rotating attacker-controlled `from` peer IDs followed by matching `ConnectionSync` messages. Because `content.from` is never validated against the actual session peer ID, the per-`from` interval guard is trivially bypassed. At the 30 req/s session rate limit with 24 addresses each, a single attacker connection generates ~720 open sockets per second, exhausting the default file-descriptor limit in under 2 seconds and crashing the node.

## Finding Description

**Root cause 1 — `content.from` is never validated against the actual session peer ID.**

`ConnectionRequestProcess` holds `peer: PeerIndex` (the real session identity): [1](#0-0) 

`execute()` parses `content.from` from the attacker-supplied message bytes but never cross-checks it against the peer ID associated with `self.peer`. The field `self.peer` is used only to route the reply back, not to authenticate the sender: [2](#0-1) 

**Root cause 2 — `respond_delivered` stores attacker-controlled addresses without IP-range validation.**

The filter at lines 196–215 only removes non-TCP transports; any TCP address with an `Ip4` or `Ip6` component passes — including `127.0.0.1`, `10.x.x.x`, `192.168.x.x`: [3](#0-2) 

The filtered addresses are then stored unconditionally under the attacker-supplied `from_peer_id`: [4](#0-3) 

**Root cause 3 — The `HOLE_PUNCHING_INTERVAL` guard is keyed by `from_peer_id` and is fully bypassable.**

The guard checks whether a recent entry exists for `from_peer_id`: [5](#0-4) 

Because `content.from` is never validated against the real session peer ID, the attacker supplies a fresh random `from` peer ID on every message. Each new ID has no prior entry, so the interval check always passes. The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)` and is equally bypassable: [6](#0-5) 

**Root cause 4 — `ConnectionSyncProcess` has no `peer` field and cannot authenticate the sender.**

`ConnectionSyncProcess` is constructed without a `peer` parameter: [7](#0-6) 

When `content.to == self_peer_id`, it unconditionally retrieves `pending_delivered[content.from]` and spawns `try_nat_traversal` tasks for every stored address: [8](#0-7) 

**Root cause 5 — `try_nat_traversal` holds a TCP socket for up to 30 seconds per address.** [9](#0-8) 

**Root cause 6 — `pending_delivered` grows unboundedly between 5-minute cleanup cycles.**

At 30 req/s with unique `from` IDs, ~9,000 entries (each holding up to 24 `Multiaddr` objects) accumulate per 5-minute window: [10](#0-9) 

**The only binding constraint is the per-session rate limiter (30 req/s per message type):** [11](#0-10) 

At 30 `ConnectionSync` messages/second × 24 addresses each = 720 `try_nat_traversal` tasks/second, each holding a TCP socket for up to 30 seconds → ~21,600 concurrent sockets from a single connection, exhausting the default file-descriptor limit (1024) in approximately 1.4 seconds.

## Impact Explanation

**High (10001–15000 points) — Vulnerability which could easily crash a CKB node.**

A single attacker connection exhausts the process file-descriptor limit in under 2 seconds (1024 ÷ 720 sockets/second ≈ 1.4 s), causing all subsequent socket operations — P2P connections, RPC — to fail with `EMFILE`. The secondary memory exhaustion via unbounded `pending_delivered` growth provides an independent crash vector. Both impacts are directly caused by this protocol's missing sender authentication, not by any external dependency.

## Likelihood Explanation

The attacker requires only a standard P2P connection — no special privileges, leaked keys, or majority hashpower. The two-message sequence (`ConnectionRequest` then `ConnectionSync`) is trivially constructable from the published molecule schema. The attack is immediately repeatable after reconnection and is not mitigated by any existing guard once `from` peer IDs are rotated. The `ADDRS_COUNT_LIMIT` of 24 is a protocol constant the attacker can always saturate. [12](#0-11) 

## Recommendation

1. **Validate `content.from` against the actual session peer ID**: In `ConnectionRequestProcess::execute`, look up the peer ID for `self.peer` from the peer registry and reject messages where `content.from` does not match.
2. **Filter private/loopback addresses**: In `respond_delivered` (and `try_nat_traversal`), reject any `listen_addr` whose IP component is loopback (`127.0.0.0/8`, `::1`), link-local (`169.254.0.0/16`, `fe80::/10`), or RFC-1918 private (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`).
3. **Add a `peer: PeerIndex` field to `ConnectionSyncProcess`**: Look up its peer ID and reject messages where `content.from` does not match the actual sender.
4. **Cap `pending_delivered` size**: Enforce a maximum number of entries (e.g., proportional to connected peer count) to bound memory consumption independent of the cleanup timer.

## Proof of Concept

```
1. Attacker (session peer_id=A) establishes a P2P connection to victim (peer_id=V).

2. Loop at 30 iterations/second, each with a fresh random from_id=R_N:

   a. Send ConnectionRequest {
        from: R_N,          // random, never validated against A
        to: V,              // victim's actual peer ID
        max_hops: 1,
        route: [],
        listen_addrs: [     // 24 private/loopback TCP addresses
          /ip4/127.0.0.1/tcp/6379,
          /ip4/10.0.0.1/tcp/8545,
          ... (22 more)
        ]
      }
      → Victim: HOLE_PUNCHING_INTERVAL check passes (R_N is new)
      → Victim: TCP/IPv4 filter passes (private IPs not blocked)
      → Victim: pending_delivered[R_N] = ([24 addrs], now)

   b. Send ConnectionSync {
        from: R_N,          // matches pending_delivered key
        to: V,
        route: []
      }
      → Victim: self_peer_id == content.to → passive branch
      → Victim: listens = pending_delivered[R_N] → 24 addresses
      → Victim: runtime::spawn(select_ok([24 × try_nat_traversal]))
      → Each task holds a TCP socket, retries for 30 seconds

3. After ~1.4 seconds (1024 fd / 720 sockets/second):
   - Node exhausts file descriptors
   - All subsequent socket operations fail with EMFILE
   - Node is effectively crashed / unreachable
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L110-153)
```rust
    pub(crate) async fn execute(mut self) -> Status {
        let content = match RequestContent::try_from(&self.message) {
            Ok(content) => content,
            Err(status) => return status,
        };
        if content.listen_addrs.len() > ADDRS_COUNT_LIMIT || content.listen_addrs.is_empty() {
            return StatusCode::InvalidListenAddrLen
                .with_context("the listen address count is too large or empty");
        }

        if content.max_hops > MAX_HOPS {
            return StatusCode::InvalidMaxTTL.into();
        }
        if content.route.len() > MAX_HOPS as usize {
            return StatusCode::InvalidRoute.with_context("the route length is too long");
        }

        let self_peer_id = self.protocol.network_state.local_peer_id();
        if content.route.contains(self_peer_id) {
            return StatusCode::Ignore.with_context("the message is passed, ignore it");
        }

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

        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
        } else if content.max_hops == 0u8 {
            StatusCode::ReachedMaxHops.into()
        } else {
            self.forward_message(self_peer_id, &content.to).await
        }
    }
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

**File:** network/src/protocols/hole_punching/mod.rs (L27-27)
```rust
const ADDRS_COUNT_LIMIT: usize = 24;
```

**File:** network/src/protocols/hole_punching/mod.rs (L44-47)
```rust
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
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

**File:** network/src/protocols/hole_punching/mod.rs (L173-174)
```rust
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
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

**File:** network/src/protocols/hole_punching/component/mod.rs (L65-68)
```rust
    let timeout_duration = Duration::from_secs(30);
    let start_time = Instant::now();
    let mut retry_count = 0u32;
    while start_time.elapsed() < timeout_duration {
```
