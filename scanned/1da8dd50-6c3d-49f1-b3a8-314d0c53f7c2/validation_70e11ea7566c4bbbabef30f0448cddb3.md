Audit Report

## Title
Unauthenticated `from` field in hole-punching protocol allows any connected peer to trigger outbound TCP connections to attacker-controlled addresses — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

## Summary
The hole-punching protocol's `ConnectionRequest` and `ConnectionSync` handlers accept a `from` field from message content without verifying it matches the actual sender's session peer ID. A single directly-connected P2P peer can send two crafted messages with a spoofed `from` peer ID to cause the victim node to spawn `try_nat_traversal` tasks that make repeated outbound TCP connections to arbitrary attacker-controlled IP:port combinations. By cycling through distinct `from` peer IDs, the attacker bypasses both the per-key `forward_rate_limiter` and the `HOLE_PUNCHING_INTERVAL` deduplication guard, enabling resource exhaustion sufficient to crash the node.

## Finding Description

**Step 1 — `ConnectionRequest` populates `pending_delivered` with attacker-controlled data**

In `connection_request.rs`, when `self_peer_id == &content.to` (line 145), `respond_delivered()` is called with `content.from` taken directly from message bytes. There is no check that `content.from` matches the actual session peer ID; the `peer` field (a `PeerIndex`) is only used to route the `ConnectionRequestDelivered` response back to the sender (line 226–229) and is never compared against `content.from`.

The `remote_listens` are filtered to TCP+IPv4/IPv6 only (lines 196–215), but the IP addresses themselves are fully attacker-controlled. After sending the response, the entry is inserted unconditionally:

```rust
// connection_request.rs L234-237
let now = unix_time_as_millis();
self.protocol
    .pending_delivered
    .insert(from_peer_id, (remote_listens, now));
```

The only deduplication guard is the `HOLE_PUNCHING_INTERVAL` check at lines 161–167, which is keyed by `from_peer_id` — itself attacker-controlled.

**Step 2 — `ConnectionSync` looks up `pending_delivered` by attacker-controlled `content.from` and spawns NAT traversal**

In `connection_sync.rs`, `ConnectionSyncProcess` has no `peer` field at all (struct definition lines 51–57). When `route` is empty and `self_peer_id == &content.to` (lines 98–105), the handler looks up `pending_delivered` by `content.from` (lines 111–115) and spawns `try_nat_traversal` tasks (lines 119–124) for each stored listen address. There is no authentication that the `ConnectionSync` sender is the same peer that sent the original `ConnectionRequest`, nor that either sender's `from` matches their actual peer ID.

**Step 3 — `try_nat_traversal` makes repeated TCP connections**

`try_nat_traversal` (mod.rs lines 49–115) loops for up to 30 seconds, creating a new TCP socket and calling `socket.connect(net_addr)` with a 200ms timeout on each iteration, sleeping ~200ms between attempts — approximately 75–150 connection attempts per task.

**Rate limiter bypass**

There are two rate limiters:
- `rate_limiter` (mod.rs line 45): keyed by `(PeerIndex, msg_item_id)` — limits a single session to 30 messages/second per message type. This is the only per-session bound.
- `forward_rate_limiter` (mod.rs line 46): keyed by `(content.from, content.to, msg_item_id)` — trivially bypassed by using a fresh random `from` peer ID in each message.

The `HOLE_PUNCHING_INTERVAL` check (connection_request.rs lines 161–167) is also keyed by `from_peer_id`, so it is equally bypassed.

**Attack flow:**
1. Attacker connects to victim via normal P2P handshake (single session).
2. Attacker sends `ConnectionRequest { from: random_id_N, to: victim_id, listen_addrs: [target_ip:port], route: [], max_hops: 6 }`. Victim stores `pending_delivered[random_id_N] = ([target_ip:port], now)`.
3. Attacker sends `ConnectionSync { from: random_id_N, to: victim_id, route: [] }`. Victim spawns `try_nat_traversal(bind_addr, target_ip:port)`.
4. Repeat steps 2–3 with fresh `random_id_N+1` values. The session-level `rate_limiter` allows up to 30 `ConnectionRequest` and 30 `ConnectionSync` messages per second.

After 30 seconds, up to 900 concurrent `try_nat_traversal` tasks are running, each holding an open TCP socket and making connection attempts. The `pending_delivered` map also grows at 30 entries/second (each holding up to 24 `Multiaddr` values), cleaned up only every 5 minutes (mod.rs lines 172–175), accumulating up to ~9,000 entries.

## Impact Explanation

**High — Vulnerabilities which could easily crash a CKB node.**

- **File descriptor / resource exhaustion**: 900 concurrent async tasks each holding a TCP socket, plus the OS-level SYN queue pressure, can exhaust file descriptors or ephemeral ports on the victim node, causing it to crash or become unable to accept new connections.
- **Memory exhaustion**: The `pending_delivered` HashMap grows unboundedly between cleanup intervals (up to 9,000 entries × 24 Multiaddrs each).
- **SSRF / port scanning**: The victim node emits TCP SYNs to arbitrary IP:port including RFC-1918, loopback, and third-party addresses, with the victim's IP as the apparent source.

## Likelihood Explanation

The attacker requires only a single standard P2P connection — no special privileges, no proof-of-work, no key material beyond what is established during normal P2P handshake. The two required message types are structurally valid and pass all existing format checks. The rate limiter bypass requires only generating random byte sequences for the `from` field. The attack is repeatable and scalable from a single connection.

## Recommendation

1. **Authenticate `from`**: In `ConnectionRequestProcess`, verify that `content.from` equals the peer ID of the actual session by resolving `self.peer` (the `PeerIndex`) to a `PeerId` via the peer registry and rejecting messages where `content.from != actual_sender_peer_id`. Apply the same check in `ConnectionSyncProcess` (which currently has no `peer` field and must be extended to carry one).
2. **Bound concurrent NAT traversal tasks**: Maintain an atomic counter of in-flight `try_nat_traversal` tasks in `HolePunching` and refuse to spawn new ones beyond a small limit (e.g., 8–16).
3. **Bound `pending_delivered` size**: Cap the map at a fixed maximum number of entries (e.g., 64) and reject new insertions when the cap is reached.
4. **Validate addresses against a blocklist**: Reject RFC-1918, loopback, and link-local addresses in `listen_addrs` before storing them in `pending_delivered`.

## Proof of Concept

```
1. Attacker establishes a normal P2P connection to victim node.

2. For N = 1..900 (at ≤30/sec to stay within session rate limit):
   a. Generate random_peer_id_N (any valid PeerId bytes).
   b. Send HolePunchingMessage::ConnectionRequest {
          from: random_peer_id_N,
          to: victim_peer_id,
          listen_addrs: [/ip4/192.168.1.1/tcp/22],
          route: [],
          max_hops: 6
      }
      → victim stores pending_delivered[random_peer_id_N] = ([/ip4/192.168.1.1/tcp/22], now)
   c. Wait ~50ms (ensure pending_delivered insert completes).
   d. Send HolePunchingMessage::ConnectionSync {
          from: random_peer_id_N,
          to: victim_peer_id,
          route: []
      }
      → victim spawns try_nat_traversal(bind_addr, /ip4/192.168.1.1/tcp/22)

3. Observable effect after 30 seconds:
   - tcpdump on 192.168.1.1 shows TCP SYNs originating from victim's IP.
   - victim node's open file descriptor count approaches system limit.
   - victim node's pending_delivered map holds ~9000 entries consuming significant memory.
   - victim node may become unresponsive or crash due to resource exhaustion.
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

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

**File:** network/src/protocols/hole_punching/component/mod.rs (L49-68)
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
```

**File:** network/src/protocols/hole_punching/mod.rs (L44-46)
```rust
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
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

**File:** network/src/protocols/hole_punching/mod.rs (L172-175)
```rust
        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```
