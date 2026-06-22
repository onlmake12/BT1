### Title
Predictable Ping Nonce Enables Liveness Spoofing by Any Connected Peer - (File: `network/src/protocols/ping.rs`)

### Summary
The `nonce()` function in `PingHandler` generates ping challenge nonces from elapsed wall-clock seconds rather than a CSPRNG. This is the direct CKB analog of the `IV.fromLength` issue: a value that must be unpredictable is instead deterministic. Additionally, the same nonce is broadcast to every connected peer in each round, mirroring the "IV generated twice" pattern from the report.

### Finding Description
The `nonce` function computes the challenge value as:

```rust
fn nonce(t: &Instant, start_time: Instant) -> u32 {
    t.saturating_duration_since(start_time).as_secs() as u32
}
``` [1](#0-0) 

This value is the integer number of seconds since the `PingHandler` was constructed. It is not random. In `ping_peers`, this single deterministic nonce is assigned to every connected session and broadcast to all peers simultaneously:

```rust
let send_nonce = nonce(&now, self.start_time);
// ...
ps.nonce = send_nonce;
// ...
let ping_msg = PingMessage::build_ping(send_nonce);
context.filter_broadcast(TargetSession::Multi(...), proto_id, ping_msg)
``` [2](#0-1) 

The pong validation accepts a pong as valid if and only if `nonce == status.nonce()`:

```rust
if let Some(status) = self.connected_session_ids.get_mut(&session_id)
    && (true, nonce) == (status.processing, status.nonce())
``` [3](#0-2) 

### Impact Explanation
A connected peer can:
1. Observe the nonce value from the first ping it receives (the nonce is in plaintext in the `Ping` molecule message).
2. Compute future nonce values trivially, since `nonce = floor((now - start_time).as_secs())`. The node's uptime is directly encoded in the nonce.
3. Send a `Pong` with the correct predicted nonce **before** the ping is even dispatched, or while ignoring all other protocol messages.

This lets a malicious peer pass the liveness check (`ping timeout` disconnection) without actually processing any sync, relay, or transaction messages. The peer holds a connection slot and avoids eviction while contributing nothing — or while performing a selective eclipse by occupying inbound slots.

### Likelihood Explanation
Any unprivileged peer that completes the TCP/secio handshake can exploit this. No special knowledge beyond the nonce value observed in the first ping is required. The nonce increments by 1 per second, so after one ping interval the attacker has full predictive capability. The ping interval is configurable but defaults to a short period, making this immediately reachable.

### Recommendation
Replace the time-derived nonce with a per-session, per-round cryptographically random value:

```rust
use rand::random;

fn nonce() -> u32 {
    random::<u32>()
}
```

Each `PingStatus` should store an independently generated nonce per session rather than sharing one nonce across all peers in a round.

### Proof of Concept
1. Connect to a CKB node as a peer.
2. Receive the first `Ping` message; extract nonce `N` (e.g., `N = 42`).
3. Note that the next ping round will send nonce `N+interval_secs`.
4. Before the next ping arrives, send `Pong(N+interval_secs)` to the node.
5. The node's `pong_received` handler accepts it, resets `processing = false`, and updates `ping_rtt` — the peer is considered alive despite never processing the actual ping.
6. Repeat indefinitely to hold the connection slot without doing any protocol work.

### Citations

**File:** network/src/protocols/ping.rs (L81-114)
```rust
    async fn ping_peers(&mut self, context: &ProtocolContext) {
        let now = Instant::now();
        let send_nonce = nonce(&now, self.start_time);
        let peers: HashSet<SessionId> = self
            .connected_session_ids
            .iter_mut()
            .filter_map(|(session_id, ps)| {
                if ps.processing {
                    None
                } else {
                    ps.processing = true;
                    ps.last_ping_sent_at = now;
                    ps.nonce = send_nonce;
                    Some(*session_id)
                }
            })
            .collect();
        if !peers.is_empty() {
            debug!("start ping peers: {:?}", peers);
            let ping_msg = PingMessage::build_ping(send_nonce);
            let proto_id = context.proto_id;
            if context
                .filter_broadcast(
                    TargetSession::Multi(Box::new(peers.into_iter())),
                    proto_id,
                    ping_msg,
                )
                .await
                .is_err()
            {
                debug!("Failed to send message");
            }
        }
    }
```

**File:** network/src/protocols/ping.rs (L117-119)
```rust
fn nonce(t: &Instant, start_time: Instant) -> u32 {
    t.saturating_duration_since(start_time).as_secs() as u32
}
```

**File:** network/src/protocols/ping.rs (L227-229)
```rust
                        if let Some(status) = self.connected_session_ids.get_mut(&session.id)
                            && (true, nonce) == (status.processing, status.nonce())
                        {
```
