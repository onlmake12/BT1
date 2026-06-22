### Title
Predictable Ping Nonce Derived from Elapsed Time Enables Liveness Spoofing - (File: `network/src/protocols/ping.rs`)

### Summary
The CKB P2P ping protocol generates its challenge nonce using only elapsed wall-clock seconds since the `PingHandler` was created. This value is fully deterministic, monotonically increasing, and publicly observable from any received ping message. An unprivileged connected peer can predict all future nonce values and send pong responses without actually receiving the corresponding ping, faking liveness and evading timeout-based disconnection.

### Finding Description
The `nonce` function at `network/src/protocols/ping.rs` lines 117–119 computes the ping challenge as:

```rust
fn nonce(t: &Instant, start_time: Instant) -> u32 {
    t.saturating_duration_since(start_time).as_secs() as u32
}
```

This is called in `ping_peers` (line 83) as `nonce(&Instant::now(), self.start_time)`, producing a value that is simply the integer number of seconds since the `PingHandler` was initialized. Three properties make this exploitable:

1. **Deterministic and monotonically increasing**: Like the `randomizationNonce` in the DarkMythos report, this value increments by exactly the ping interval (a fixed, observable duration) each round. Any peer that receives one ping immediately learns the node's approximate start time and can compute every future nonce.

2. **Identical nonce broadcast to all peers**: Lines 83–93 compute `send_nonce` once and assign it to every connected session before the ping is sent. A peer that receives a ping therefore knows the nonce that was sent to every other peer in that round.

3. **`processing` flag is set before the ping is transmitted**: Line 91 sets `ps.processing = true` and line 93 sets `ps.nonce = send_nonce` inside the iterator that builds the peer set, which completes before `filter_broadcast` is called on line 103. This means the node's internal state already accepts a pong for the predicted nonce before the ping message has left the node.

The pong validation at lines 227–228 checks only two things:

```rust
if let Some(status) = self.connected_session_ids.get_mut(&session.id)
    && (true, nonce) == (status.processing, status.nonce())
```

A wrong nonce causes immediate disconnection (lines 236–244). A correct nonce — whether legitimately echoed or pre-computed — resets `processing` to `false` and updates the peer's liveness timestamp via `pong_received`.

### Impact Explanation
A malicious connected peer can maintain a "zombie" connection indefinitely: it observes the first ping nonce, derives the node's start time, and thereafter sends correctly-predicted pong messages timed to arrive after `processing` is set but before the actual ping message is processed. The node's liveness tracking is deceived — the peer appears healthy, its `ping_rtt` is updated, and it is never disconnected for timeout. This occupies a connection slot in the peer registry without the peer actually participating in the protocol, which can be used to degrade the node's effective peer connectivity or as a component of an eclipse attack.

### Likelihood Explanation
The attack requires only an established P2P connection — no privileged role, no key material, no majority hashpower. The nonce is transmitted in plaintext in every ping message, so a single observed ping reveals the node's start time. The ping interval is fixed and observable from message timing. The attacker needs only to send a pong message with the correct predicted `u32` value at the right moment, which is straightforward to implement.

### Recommendation
Replace the deterministic elapsed-time nonce with a cryptographically random value generated per ping round using `rand::thread_rng()` (already a dependency in the crate, used elsewhere in the network layer). The nonce should be generated fresh for each ping round and should not be derived from any publicly observable or predictable quantity.

```rust
fn nonce() -> u32 {
    rand::thread_rng().gen()
}
```

### Proof of Concept

1. Attacker connects to a CKB node as a peer.
2. Attacker receives the first `Ping` message containing nonce `N` (e.g., `N = 3600`, meaning the node has been running ~3600 seconds).
3. Attacker records the wall-clock time `t0` at which the ping arrived and the ping interval `T` (observable from subsequent pings).
4. Attacker computes the next expected nonce: `N_next = N + T` (e.g., `3615` for a 15-second interval).
5. At time `t0 + T`, the attacker sends a `Pong` message with nonce `N_next` to the node.
6. The node has already set `ps.processing = true` and `ps.nonce = N_next` (line 91–93) before broadcasting the ping. The attacker's pong arrives with the correct nonce and `processing == true`, so the check at line 228 passes.
7. The node records the peer as alive (`pong_received` is called), resets `processing = false`, and never triggers the timeout disconnection path.
8. The attacker repeats step 4–7 indefinitely, maintaining a live-appearing connection without processing any real protocol messages. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** network/src/protocols/ping.rs (L81-93)
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
