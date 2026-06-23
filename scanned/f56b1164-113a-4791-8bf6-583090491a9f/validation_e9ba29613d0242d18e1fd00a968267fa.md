### Title
Predictable Global Ping Nonce Enables RTT Manipulation and Eviction Protection Bypass - (File: `network/src/protocols/ping.rs`)

### Summary

The `PingHandler` in CKB's P2P ping protocol computes a single time-based nonce shared across all connected peers in each ping round. Because the nonce is derived deterministically from elapsed seconds since node start, any connected peer can predict all future nonces. This allows a malicious peer to pre-compute and time its pong responses to fake a near-zero RTT, directly undermining the eviction protection mechanism that explicitly relies on ping RTT being hard to manipulate.

### Finding Description

In `network/src/protocols/ping.rs`, the `nonce` function computes the ping challenge as:

```rust
fn nonce(t: &Instant, start_time: Instant) -> u32 {
    t.saturating_duration_since(start_time).as_secs() as u32
}
``` [1](#0-0) 

This produces a value equal to the number of whole seconds the node has been running. In `ping_peers`, a **single** `send_nonce` is computed once and assigned to every peer being pinged in that round:

```rust
let send_nonce = nonce(&now, self.start_time);
// ...
ps.nonce = send_nonce;   // same value for every peer
``` [2](#0-1) 

The same message is then broadcast to all peers simultaneously:

```rust
let ping_msg = PingMessage::build_ping(send_nonce);
context.filter_broadcast(TargetSession::Multi(...), proto_id, ping_msg).await
``` [3](#0-2) 

The pong validation checks only two things: that `processing == true` and that the nonce matches the stored value:

```rust
if let Some(status) = self.connected_session_ids.get_mut(&session.id)
    && (true, nonce) == (status.processing, status.nonce())
``` [4](#0-3) 

Because the nonce is `elapsed_seconds_since_start`, a peer that receives one ping immediately learns the formula. With a default `ping_interval_secs = 120`, the next nonce will be exactly `current_nonce + 120`. The peer can pre-compute this value and send the pong within milliseconds of the ping being dispatched, achieving a measured RTT of effectively 0 seconds regardless of actual network latency.

### Impact Explanation

The eviction logic in `peer_registry.rs` explicitly protects peers with the lowest ping RTT, with a code comment stating this is a characteristic "an attacker hard to simulate or manipulate":

```rust
// Protect peers based on characteristics that an attacker hard to simulate or manipulate
// Protect peers which has the lowest ping
sort_then_drop(&mut candidate_peers, EVICTION_PROTECT_PEERS, |peer1, peer2| {
    let peer1_ping = peer1.ping_rtt.map(|p| p.as_secs()).unwrap_or_else(|| u64::MAX);
    ...
``` [5](#0-4) 

The predictable nonce directly violates this assumption. A malicious peer can:

1. Connect to the node and receive ping with nonce `N` at time `T`.
2. Compute that the next ping will carry nonce `N + 120` at time `T + 120`.
3. At `T + 120 + ε`, send `pong(N + 120)` immediately — the `processing` flag is now `true` and the nonce matches.
4. The node records `ping_rtt ≈ ε` (sub-second, stored as 0 seconds by `as_secs()`).
5. The peer is placed in the protected set and cannot be evicted.

This allows an attacker to permanently occupy inbound connection slots, displacing legitimate peers and degrading the node's peer diversity and sync quality.

### Likelihood Explanation

The attack requires only that a peer:
- Connect to the node (no privilege required — any unprivileged inbound peer qualifies).
- Observe one ping to learn the node's `start_time` offset.
- Implement a simple timer to send pre-computed pongs.

The default `ping_interval_secs = 120` and `ping_timeout_secs = 1200` give the attacker a 1200-second window to respond, making timing trivial. [6](#0-5) 

The `ping_peers` RPC endpoint (`NetworkController::ping_peers`) is also callable by any local RPC user, which can be used to force an immediate ping round at a known time, further simplifying the timing attack. [7](#0-6) 

### Recommendation

Replace the global time-based nonce with a **per-peer cryptographically random nonce** generated independently for each session at ping time. Store the random nonce in `PingStatus` (which already has a `nonce: u32` field) and remove the shared `send_nonce` computation:

```rust
// Instead of:
let send_nonce = nonce(&now, self.start_time);
ps.nonce = send_nonce;

// Use:
use rand::Rng;
ps.nonce = rand::thread_rng().gen::<u32>();
// Build a per-peer message using ps.nonce
``` [8](#0-7) 

This eliminates predictability and ensures each peer faces a unique, unguessable challenge per round, restoring the security property the eviction logic depends on.

### Proof of Concept

1. Connect to a CKB node as an inbound peer.
2. Receive the first `Ping` message; decode its `nonce` field (little-endian `u32`). Call this `N`. Record the local time `T`.
3. Compute `next_nonce = N + ping_interval_secs` (default: `N + 120`).
4. At time `T + 120 + 50ms`, send a `Pong` message with `nonce = next_nonce` on the same session.
5. The node's `received` handler checks `processing == true` (set when the ping was dispatched at `T + 120`) and `nonce == next_nonce` — both pass.
6. `pong_received` is called; `peer.ping_rtt` is set to ~50ms.
7. Repeat every 120 seconds. The peer's RTT is permanently recorded as ~50ms regardless of actual latency, placing it in the eviction-protected set. [9](#0-8) [5](#0-4)

### Citations

**File:** network/src/protocols/ping.rs (L32-38)
```rust
pub struct PingHandler {
    interval: Duration,
    timeout: Duration,
    connected_session_ids: HashMap<SessionId, PingStatus>,
    network_state: Arc<NetworkState>,
    control_receiver: Receiver<()>,
    start_time: Instant,
```

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

**File:** network/src/protocols/ping.rs (L225-233)
```rust
                    PingPayload::Pong(nonce) => {
                        // check pong
                        if let Some(status) = self.connected_session_ids.get_mut(&session.id)
                            && (true, nonce) == (status.processing, status.nonce())
                        {
                            status.processing = false;
                            let last_ping_sent_at = status.last_ping_sent_at;
                            self.pong_received(session.id, last_ping_sent_at);
                            return;
```

**File:** network/src/peer_registry.rs (L149-165)
```rust
        // Protect peers based on characteristics that an attacker hard to simulate or manipulate
        // Protect peers which has the lowest ping
        sort_then_drop(
            &mut candidate_peers,
            EVICTION_PROTECT_PEERS,
            |peer1, peer2| {
                let peer1_ping = peer1
                    .ping_rtt
                    .map(|p| p.as_secs())
                    .unwrap_or_else(|| u64::MAX);
                let peer2_ping = peer2
                    .ping_rtt
                    .map(|p| p.as_secs())
                    .unwrap_or_else(|| u64::MAX);
                peer2_ping.cmp(&peer1_ping)
            },
        );
```

**File:** network/src/network.rs (L1598-1603)
```rust
    /// Try ping all connected peers
    pub fn ping_peers(&self) {
        if let Some(mut ping_controller) = self.ping_controller.clone() {
            let _ignore = ping_controller.try_send(());
        }
    }
```
