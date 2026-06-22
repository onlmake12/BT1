### Title
Predictable Time-Based Ping Nonce Allows Any Connected Peer to Forge Liveness and Permanently Occupy Connection Slots - (File: `network/src/protocols/ping.rs`)

### Summary

The CKB P2P ping protocol uses a nonce derived purely from wall-clock elapsed seconds since node startup (`t.saturating_duration_since(start_time).as_secs() as u32`). This nonce is not random: it is fully deterministic and recoverable by any peer from the first ping it receives. Because the same nonce is broadcast to every connected peer simultaneously, a malicious peer can compute all future nonces, send pre-computed pong replies without ever processing the actual ping message, and permanently avoid the ping-timeout disconnection mechanism. This allows an attacker to hold connection slots open indefinitely while being completely unresponsive to all other protocol traffic.

### Finding Description

**Root cause — predictable nonce generation:**

```rust
// network/src/protocols/ping.rs:117-118
fn nonce(t: &Instant, start_time: Instant) -> u32 {
    t.saturating_duration_since(start_time).as_secs() as u32
}
```

The nonce is the integer number of seconds the node has been running. It carries zero entropy. [1](#0-0) 

**Same nonce broadcast to all peers simultaneously:**

```rust
// network/src/protocols/ping.rs:81-113
async fn ping_peers(&mut self, context: &ProtocolContext) {
    let now = Instant::now();
    let send_nonce = nonce(&now, self.start_time);   // single value
    let peers: HashSet<SessionId> = self
        .connected_session_ids
        .iter_mut()
        .filter_map(|(session_id, ps)| {
            ...
            ps.nonce = send_nonce;   // same nonce for every peer
            ...
        })
        .collect();
    let ping_msg = PingMessage::build_ping(send_nonce);
    context.filter_broadcast(..., ping_msg, ...).await;
}
``` [2](#0-1) 

**Pong validation only checks `processing == true` and nonce equality:**

```rust
// network/src/protocols/ping.rs:225-234
PingPayload::Pong(nonce) => {
    if let Some(status) = self.connected_session_ids.get_mut(&session.id)
        && (true, nonce) == (status.processing, status.nonce())
    {
        status.processing = false;
        ...
        self.pong_received(session.id, last_ping_sent_at);
        return;
    }
    // disconnect on mismatch
}
``` [3](#0-2) 

**End-to-end exploit path:**

1. Attacker connects to the target CKB node over P2P.
2. The node sends a ping with nonce `N = elapsed_seconds_since_start`. The attacker receives this message and immediately learns `N`.
3. From `N` and the current wall time, the attacker recovers the node's `start_time`: `start_time ≈ now − N seconds`.
4. The attacker can now compute every future nonce exactly: `future_nonce = (future_time − start_time).as_secs() as u32`.
5. When the next ping interval fires (default 120 s, `ping_interval_secs = 120`), the attacker's process sends a pong carrying the pre-computed nonce **without reading or processing the actual ping message**.
6. The node's pong handler sees `processing == true` and `nonce matches`, clears `processing`, and records a fresh `last_ping_protocol_message_received_at`. The peer is considered alive.
7. The attacker repeats step 5 indefinitely, holding the connection slot open while ignoring all sync, relay, and other protocol messages. [4](#0-3) 

### Impact Explanation

The ping/pong protocol is the **only** mechanism in `PingHandler` that disconnects unresponsive peers (the `CHECK_TIMEOUT_TOKEN` path). By forging valid pong replies, an attacker permanently disables this eviction path for its own session. [5](#0-4) 

Concrete consequences for an unprivileged attacker:

- **Connection-slot exhaustion**: CKB nodes have a bounded `max_peers` (default 125) and `max_outbound_peers` (default 8). An attacker holding slots open with fake liveness prevents legitimate peers from connecting, degrading the node's view of the network and its ability to receive blocks and transactions.
- **Eclipse-attack facilitation**: If the attacker controls enough inbound connections (reachable without Sybil because inbound slots are open to any peer), it can monopolize the node's peer set while appearing alive, cutting the node off from honest peers.
- **RTT manipulation**: `pong_received` records `ping_rtt` used for peer scoring. The attacker can send the pong at an arbitrary time, falsifying its measured latency. [6](#0-5) 

### Likelihood Explanation

- **No privilege required**: any TCP-reachable peer can connect and execute this attack.
- **Trivial to implement**: after receiving one ping, the attacker has everything needed to predict all future nonces. The formula is a single subtraction and integer cast.
- **Persistent**: the attack works for the entire lifetime of the node process because `start_time` never changes.
- **Undetectable**: from the node's perspective the peer looks perfectly healthy.

### Recommendation

Replace the time-based nonce with a cryptographically random value generated per-ping, per-peer:

```rust
use rand::RngCore;

fn nonce() -> u32 {
    rand::thread_rng().next_u32()
}
```

Generate a fresh random nonce for **each peer individually** inside `ping_peers`, store it in `PingStatus.nonce`, and validate it on pong receipt as today. This ensures that:
- No peer can predict another peer's nonce.
- No peer can predict its own future nonces.
- Forging a valid pong requires guessing a 32-bit random value (1-in-4-billion per attempt).

### Proof of Concept

```python
import socket, time, struct

# Step 1: connect to CKB P2P port and complete tentacle handshake (omitted for brevity)
# Step 2: receive first Ping message, extract nonce N
first_ping_nonce = receive_ping()          # e.g. N = 3600 (node up 1 hour)
node_start_epoch = time.time() - first_ping_nonce  # recover start_time

# Step 3: for every subsequent ping interval (120 s default):
while True:
    time.sleep(120)
    predicted_nonce = int(time.time() - node_start_epoch)
    send_pong(predicted_nonce)             # node accepts, marks peer alive
    # Never read or process any other protocol message
```

The node's `CHECK_TIMEOUT_TOKEN` handler will never fire for this session because `status.processing` is reset to `false` by each accepted pong before the timeout window expires. [1](#0-0) [7](#0-6) [8](#0-7)

### Citations

**File:** network/src/protocols/ping.rs (L62-79)
```rust
    fn ping_received(&mut self, id: SessionId) {
        trace!("received ping from: {:?}", id);
        self.network_state.with_peer_registry_mut(|reg| {
            if let Some(peer) = reg.get_peer_mut(id) {
                peer.last_ping_protocol_message_received_at = Some(Instant::now());
            }
        });
    }

    fn pong_received(&mut self, id: SessionId, last_ping: Instant) {
        let now = Instant::now();
        self.network_state.with_peer_registry_mut(|reg| {
            if let Some(peer) = reg.get_peer_mut(id) {
                peer.ping_rtt = Some(now.saturating_duration_since(last_ping));
                peer.last_ping_protocol_message_received_at = Some(now);
            }
        });
    }
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

**File:** network/src/protocols/ping.rs (L225-244)
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
                        }
                        // if nonce is incorrect or can't find ping info
                        if let Err(err) = async_disconnect_with_message(
                            context.control(),
                            session.id,
                            "ping failed",
                        )
                        .await
                        {
                            debug!("Disconnect failed {:?}, error: {:?}", session.id, err);
                        }
```

**File:** network/src/protocols/ping.rs (L251-270)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, token: u64) {
        match token {
            SEND_PING_TOKEN => self.ping_peers(context).await,
            CHECK_TIMEOUT_TOKEN => {
                let timeout = self.timeout;
                for (id, _ps) in self
                    .connected_session_ids
                    .iter()
                    .filter(|(_id, ps)| ps.processing && ps.elapsed() >= timeout)
                {
                    debug!("Ping timeout, {:?}", id);
                    if let Err(err) =
                        async_disconnect_with_message(context.control(), *id, "ping timeout").await
                    {
                        debug!("Disconnect failed {:?}, error: {:?}", id, err);
                    }
                }
            }
            _ => panic!("unknown token {token}"),
        }
```
