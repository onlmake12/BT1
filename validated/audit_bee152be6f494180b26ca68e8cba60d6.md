Audit Report

## Title
Unauthenticated `from` Field in `ConnectionRequest` Enables `pending_delivered` Poisoning via Identity Spoofing — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary
`ConnectionRequestProcess::execute` accepts the `from` field verbatim from the wire message and writes it as the key into `pending_delivered` without verifying it against the actual `PeerId` of the sending session. An attacker with a single authenticated P2P connection can spoof arbitrary victim `PeerId` values in `from`, poison `pending_delivered` with attacker-controlled addresses, and then trigger `ConnectionSync` to cause the target node to spawn unbounded 30-second TCP retry loops (`try_nat_traversal`) to attacker-controlled endpoints, exhausting socket and async-task resources.

## Finding Description

**Root cause**: `RequestContent::try_from` at `connection_request.rs` L36–38 parses `from` purely from wire bytes:

```rust
let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
    StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
})?;
``` [1](#0-0) 

`ConnectionRequestProcess` stores only `peer: PeerIndex` (a session-ID integer), never the session's `PeerId`: [2](#0-1) 

In `execute`, when `self_peer_id == &content.to`, `respond_delivered` is called with the wire-supplied `content.from` verbatim: [3](#0-2) 

Inside `respond_delivered`, the attacker-controlled `from_peer_id` and `remote_listens` are written unconditionally into `pending_delivered`: [4](#0-3) 

The session's actual `PeerId` is never looked up from the peer registry and compared against `content.from`. The `peer` field is only used to route the reply back to the sender session (L226–229).

**`ConnectionSync` has the identical missing check** — `SyncContent::try_from` at `connection_sync.rs` L42–44 also parses `from` from wire bytes without session-identity verification: [5](#0-4) 

`ConnectionSyncProcess::execute` then looks up `pending_delivered.get(&content.from)` (L111–115) and spawns `try_nat_traversal` tasks on the stored addresses (L119–124): [6](#0-5) 

`try_nat_traversal` runs a TCP retry loop for up to 30 seconds per address: [7](#0-6) 

**Why existing guards fail**:

- `rate_limiter` (keyed by `(session_id, msg.item_id)`, 30 req/sec) limits per-session message rate but still allows 30 `ConnectionRequest` + 30 `ConnectionSync` messages per second from a single session: [8](#0-7) 

- `forward_rate_limiter` (keyed by `(from, to, item_id)`, 1 req/sec) is bypassed entirely by rotating victim `from` PeerIds — each new spoofed `from` gets its own fresh bucket: [9](#0-8) 

- `HOLE_PUNCHING_INTERVAL` (2 min) prevents re-insertion for the *same* `from_peer_id` but is bypassed by rotating through distinct victim PeerIds: [10](#0-9) 

## Impact Explanation

At 30 `ConnectionSync` messages/sec (the per-session cap), each carrying up to `ADDRS_COUNT_LIMIT = 24` addresses, the attacker spawns up to 720 concurrent `try_nat_traversal` async tasks per second. Each task holds a TCP socket and runs for up to 30 seconds, yielding up to ~21,600 concurrent tasks sustained from a single P2P connection. This exhausts the target node's OS socket budget and async-task resources, causing it to drop legitimate connections and degrade or halt normal P2P operation.

This matches the allowed bounty impact: **High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** [11](#0-10) 

## Likelihood Explanation

Preconditions: one standard P2P connection to the target node. No special privileges, no leaked keys, no majority hashpower. `ConnectionRequest` and `ConnectionSync` are standard production protocol messages. The spoofed `from` field passes all existing validation because the only structural check is that it decodes as a valid `PeerId`. The attack is locally reproducible with a test peer. The attacker sustains the attack indefinitely by cycling victim PeerIds to bypass the 2-minute cooldown.

## Recommendation

In `ConnectionRequestProcess::execute` (or in `received` before dispatch), look up the session's actual `PeerId` from the peer registry using `self.peer` (`PeerIndex`) via `network_state.with_peer_registry(|reg| reg.get_peer(self.peer))`, extract the `PeerId`, and assert it equals `content.from`. Reject (and optionally ban) the session if they differ.

Apply the same fix to `ConnectionSyncProcess` — add a `peer: PeerIndex` field and perform the same session-identity check on `content.from`.

For forwarded messages (where `from` is a remote originator, not the immediate sender), enforce the `from == session PeerId` invariant **only for the first hop** (i.e., when `route` is empty), since relay nodes legitimately forward messages on behalf of the original sender. The `route` field already records the forwarding path and distinguishes first-hop from relay cases. [12](#0-11) 

## Proof of Concept

```
1. Attacker node (PeerId=A) establishes a standard P2P connection to target T.

2. Attacker sends ConnectionRequest:
     from         = victim_peer_id B (any syntactically valid PeerId, not A)
     to           = T's own PeerId
     listen_addrs = [attacker_ip:attacker_port/p2p/B]  (up to 24 addrs)
     max_hops     = 1
     route        = []

3. T's ConnectionRequestProcess::execute:
   - content.from = B (wire-supplied, unchecked against session PeerId A)
   - self_peer_id == content.to → calls respond_delivered(B, T, [attacker_addr])
   - pending_delivered.insert(B, ([attacker_addr], now))

4. Attacker sends ConnectionSync:
     from  = B
     to    = T
     route = []

5. T's ConnectionSyncProcess::execute:
   - self_peer_id == content.to
   - listens_info = pending_delivered.get(B) = [attacker_addr]
   - spawns try_nat_traversal(bind_addr, attacker_addr) — 30-second TCP retry loop per address

6. Attacker repeats steps 2–5 with fresh victim PeerIds (C, D, E, …) at up to 30/sec
   to bypass HOLE_PUNCHING_INTERVAL and forward_rate_limiter, continuously exhausting
   T's socket and async-task resources.

Verification: add a debug assertion after pending_delivered.insert() confirming
from_peer_id=B while the actual session PeerId is A.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L128-130)
```rust
        if content.route.contains(self_peer_id) {
            return StatusCode::Ignore.with_context("the message is passed, ignore it");
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

**File:** network/src/protocols/hole_punching/component/mod.rs (L65-68)
```rust
    let timeout_duration = Duration::from_secs(30);
    let start_time = Instant::now();
    let mut retry_count = 0u32;
    while start_time.elapsed() < timeout_duration {
```

**File:** network/src/protocols/hole_punching/mod.rs (L27-27)
```rust
const ADDRS_COUNT_LIMIT: usize = 24;
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
