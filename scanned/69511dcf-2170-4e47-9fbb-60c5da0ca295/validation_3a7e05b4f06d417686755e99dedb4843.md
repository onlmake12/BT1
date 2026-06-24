Audit Report

## Title
Spoofed `from` Field in Hole-Punching Messages Bypasses Forward Rate Limiter and Enables Outbound Connection Exhaustion — (`File: network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary

In CKB's hole-punching protocol, the `from` peer ID in `ConnectionRequest`, `ConnectionRequestDelivered`, and `ConnectionSync` messages is parsed exclusively from the attacker-controlled message payload, with no verification against the actual transport-layer session identity. This allows any connected peer to rotate arbitrary spoofed `from` values, producing a unique `forward_rate_limiter` key per message and rendering the forward rate limiter completely ineffective. The same spoofing enables a two-step attack that causes the victim node to initiate unbounded outbound TCP connection tasks to attacker-specified IP addresses, exhausting connection resources.

## Finding Description

**Root cause — `from` parsed from payload, never verified against session:**

In `ConnectionRequestProcess::execute()`, `content.from` is deserialized from the message body: [1](#0-0) 

The actual sending session (`self.peer`, a `PeerIndex`) is available and used for the per-connection `rate_limiter`, but is never compared against `content.from`. The same pattern exists identically in `ConnectionRequestDeliveredProcess` and `ConnectionSyncProcess`: [2](#0-1) 

**Impact 1 — Forward rate-limiter bypass and network amplification:**

The `forward_rate_limiter` is keyed on `(content.from, content.to, msg_item_id)`: [3](#0-2) 

It is configured at 1 req/sec per key: [4](#0-3) 

Because `content.from` is fully attacker-controlled, rotating a fresh random `from` per message produces a unique key each time, making every message pass the limiter. The per-connection `rate_limiter` (keyed on `(session_id, item_id)`, capped at 30/sec) still applies: [5](#0-4) 

However, each of those 30 messages/sec is then gossip-broadcast to `sqrt(N)` relay peers, each of which also passes the `forward_rate_limiter` (unique key never seen before) and re-broadcasts to `sqrt(N)` more peers: [6](#0-5) 

This produces network-wide amplification from a single attacker connection.

**Impact 2 — Outbound TCP connection exhaustion:**

When the victim is the `to` target of a `ConnectionRequest`, `respond_delivered` stores attacker-supplied `listen_addrs` under the spoofed `from` key: [7](#0-6) 

The deduplication guard at L161-166 is also keyed on `from_peer_id`, so rotating `from` bypasses it too: [8](#0-7) 

When a `ConnectionSync` arrives with the same spoofed `from`, the victim retrieves those stored addresses and spawns `try_nat_traversal` tasks: [9](#0-8) 

`try_nat_traversal` runs a TCP connect-retry loop for up to 30 seconds per address, with up to 24 addresses per message (`ADDRS_COUNT_LIMIT`): [10](#0-9) 

At 30 msg/sec (per-connection rate limit), the attacker spawns up to 720 concurrent long-lived TCP tasks per second, each retrying for 30 seconds. This exhausts async task and socket resources, leading to node crash or severe degradation.

## Impact Explanation

Two concrete in-scope impacts:

1. **High — CKB network congestion with few costs**: The `forward_rate_limiter` is the sole throttle on message relay. With `from` spoofing, one attacker connection generates 30 uniquely-keyed messages/sec, each broadcast to `sqrt(N)` relay nodes, each of which re-broadcasts without throttling. This is a low-cost, high-amplification network flood.

2. **High — Crash a CKB node**: The victim node spawns up to 720 unbounded async TCP tasks per second (30 msg/sec × 24 addrs), each running for 30 seconds. This exhausts the async runtime's task pool and OS socket descriptors, crashing or hanging the node.

## Likelihood Explanation

Any peer that can establish a single P2P connection to a CKB node can send `HolePunching` protocol messages. The protocol is enabled by default (registered unconditionally for non-WASM targets). No special privilege, key, or hashpower is required. The attack requires only crafting a valid molecule-encoded `ConnectionRequest` with an arbitrary `from` field, which is trivial. The victim's peer ID is publicly discoverable via the P2P network.

## Recommendation

1. **Verify `from` against the actual session identity**: In all three `Process::execute()` methods, look up the peer ID for `self.peer` via the peer registry and assert it equals `content.from`. Reject messages where they do not match.
2. **Key `forward_rate_limiter` on the actual `PeerIndex`** (`self.peer`) rather than on payload-supplied `content.from`, mirroring the existing per-connection `rate_limiter` design.
3. **Cap concurrent `try_nat_traversal` tasks** per session or globally to bound resource consumption regardless of message rate.

## Proof of Concept

```
Attacker (directly connected to victim):

Step 1: For i in 0..N (N = 30 per second, limited by rate_limiter):
  Send HolePunchingMessage::ConnectionRequest {
    from: <random_peer_id_i>,        // fresh random ID each iteration
    to:   <victim_peer_id>,
    listen_addrs: [/ip4/192.168.1.1/tcp/8080, ... up to 24 addrs],
    max_hops: 0,                     // prevent forwarding, target victim directly
    route: [],
  }
  → Victim: respond_delivered() stores pending_delivered[random_peer_id_i] = ([192.168.1.1:8080,...], now)
  → forward_rate_limiter passes (unique key each time)

Step 2: For each random_peer_id_i:
  Send HolePunchingMessage::ConnectionSync {
    from: <random_peer_id_i>,
    to:   <victim_peer_id>,
    route: [],
  }
  → Victim: looks up pending_delivered[random_peer_id_i], finds attacker IPs
  → Spawns try_nat_traversal() tasks → 24 outbound TCP tasks × 30/sec = 720 tasks/sec
  → Each task retries for 30 seconds → ~21,600 concurrent tasks after 30s → resource exhaustion

For amplification: set max_hops=6, route=[] with to=<unknown_peer_id>
  → Each of 30 msg/sec is broadcast to sqrt(N) relay nodes
  → Each relay node passes forward_rate_limiter (unique from key) and re-broadcasts
  → Exponential fan-out across the network
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L161-166)
```rust
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
            }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L280-305)
```rust
                let sid = self.peer;
                let mut total = self
                    .protocol
                    .network_state
                    .with_peer_registry(|p| p.peers().len())
                    .isqrt();
                if let Err(error) = self
                    .p2p_control
                    .filter_broadcast(
                        TargetSession::Filter(Box::new(move |id| {
                            if id == &sid {
                                return false;
                            }
                            total = total.saturating_sub(1);
                            total != 0
                        })),
                        proto_id,
                        new_message,
                    )
                    .await
                {
                    StatusCode::BroadcastError.with_context(error)
                } else {
                    Status::ok()
                }
            }
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L42-44)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
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

**File:** network/src/protocols/hole_punching/mod.rs (L255-257)
```rust
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
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
