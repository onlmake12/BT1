Audit Report

## Title
Unauthenticated `pending_delivered` Poisoning via Spoofed `ConnectionRequest.from` Enables Unbounded Background Task Spawning and Attacker-Directed Outbound TCP Connections — (`network/src/protocols/hole_punching/component/connection_request.rs`, `connection_sync.rs`)

## Summary

`ConnectionRequestProcess` and `ConnectionSyncProcess` accept the `from` field verbatim from the wire without verifying it matches the authenticated session peer ID. An attacker with a single P2P connection can poison `pending_delivered` with an arbitrary peer ID mapped to attacker-controlled addresses, then immediately trigger `try_nat_traversal` to those addresses. By rotating the spoofed `from` value, the attacker bypasses the `forward_rate_limiter` and spawns background tasks at the session rate limit (30/s), each making ~150 outbound TCP connections over 30 seconds, leading to file descriptor exhaustion and node crash.

## Finding Description

**Root cause — no `from` ↔ session binding in `respond_delivered`:**

`ConnectionRequestProcess::execute()` calls `respond_delivered(content.from, ...)` where `content.from` is taken verbatim from the wire: [1](#0-0) 

Inside `respond_delivered`, the attacker-supplied `from_peer_id` and `remote_listens` are written directly into `pending_delivered` with no check that `from_peer_id` equals the authenticated peer ID of the sending session: [2](#0-1) 

The `ConnectionRequestProcess` struct holds `peer: PeerIndex` (the authenticated session index) but it is never consulted to validate `content.from`: [3](#0-2) 

**Root cause — no `from` ↔ session binding in `ConnectionSyncProcess`:**

`ConnectionSyncProcess::execute()` reads `content.from` from the wire and looks it up in `pending_delivered` without verifying it matches the sending session: [4](#0-3) 

The retrieved addresses are passed directly to `try_nat_traversal` inside a spawned background task: [5](#0-4) 

**`try_nat_traversal` makes real outbound TCP connections:**

The function opens a new TCP socket and calls `connect()` every ~200 ms for up to 30 seconds (~150 attempts per invocation): [6](#0-5) 

**Rate-limiter bypass:**

The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)`: [7](#0-6) 

The attacker rotates `from` (any random bytes that parse as a valid `PeerId`) to get a fresh bucket for each pair of messages, bypassing the 1 req/s per `(from, to)` limit entirely. The only binding constraint becomes the per-session `rate_limiter` at 30 msg/s: [8](#0-7) 

The `HOLE_PUNCHING_INTERVAL` deduplication check in `respond_delivered` is also bypassed by rotating `from`: [9](#0-8) 

**Unbounded task accumulation:**

At 30 msg/s, the attacker sends 15 `(ConnectionRequest, ConnectionSync)` pairs per second. Each pair spawns one background task lasting 30 seconds. After 30 seconds of sustained attack: 15 tasks/s × 30 s = 450 concurrent background tasks, each creating a new TCP socket every ~200 ms. This exhausts the process's file descriptor limit and crashes the node.

## Impact Explanation

**High — Vulnerabilities which could easily crash a CKB node.**

A single attacker session can sustain 15 new background tasks per second indefinitely. Each task holds open a TCP socket for up to 30 seconds and creates a new one every ~200 ms. The resulting file descriptor exhaustion causes the node process to fail on any subsequent socket allocation (including normal P2P connections), crashing the node. Additionally, the victim node is directed to make outbound TCP connections to arbitrary IP:port pairs (including RFC-1918 addresses), constituting attacker-directed port scanning and potential internal network probing.

## Likelihood Explanation

- Requires only a single outbound TCP connection to any CKB node with hole-punching enabled.
- No cryptographic material, no privileged role, no hashpower required.
- Both messages are structurally valid and pass all existing validation checks.
- The rate-limiter bypass via `from` rotation is trivial: generate random bytes of the correct length.
- Locally reproducible against a single node.

## Recommendation

In `respond_delivered`, resolve the authenticated `PeerId` for `self.peer` from the peer registry and assert it equals `from_peer_id` before writing to `pending_delivered`:

```rust
// In ConnectionRequestProcess::respond_delivered:
let session_peer_id = self.protocol.network_state
    .peer_registry.read()
    .get_key_by_peer_id_from_session(self.peer); // or equivalent lookup

if session_peer_id.as_ref() != Some(&from_peer_id) {
    return StatusCode::InvalidFromPeerId
        .with_context("from peer id does not match session peer id");
}
```

Apply the same check in `ConnectionSyncProcess::execute` — verify `content.from` matches the sending session's authenticated peer ID before consulting `pending_delivered`. This eliminates the spoofing vector entirely and makes the rate limiter effective.

## Proof of Concept

```
1. Start node X with hole-punching enabled.
2. Connect attacker session A (peer_id = attacker_id) to X.
3. In a loop (up to 30 req/s):
   a. Generate a fresh random victim_id (valid PeerId bytes).
   b. Send: ConnectionRequest { from: victim_id, to: X, listen_addrs: [attacker_ip:1234] }
      → respond_delivered writes pending_delivered[victim_id] = ([attacker_ip:1234], now)
   c. Send: ConnectionSync { from: victim_id, to: X, route: [] }
      → pending_delivered.get(victim_id) → try_nat_traversal spawned as background task
      → background task makes TCP SYN to attacker_ip:1234 every ~200ms for 30s
4. After ~30 seconds: 450 concurrent background tasks each holding/creating TCP sockets.
5. Observe: node X fails to open new sockets → crashes or becomes unresponsive.

Invariant violated:
  pending_delivered[victim_id] must only be populated when the session
  that sent the ConnectionRequest has session_peer_id == victim_id.
  Step 3b populates it with attacker_id session but victim_id key — invariant broken.
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-115)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L119-124)
```rust
                            let tasks = listens
                                .into_iter()
                                .map(|listen_addr| {
                                    Box::pin(try_nat_traversal(self.bind_addr, listen_addr))
                                })
                                .collect::<Vec<_>>();
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L62-85)
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
