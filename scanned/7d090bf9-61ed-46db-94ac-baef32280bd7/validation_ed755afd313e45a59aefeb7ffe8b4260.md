### Title
Missing Peer Direction Authentication in `NetTimeProtocol::received` Allows Inbound Peers to Poison Time-Offset Samples — (`sync/src/net_time_checker.rs`)

---

### Summary

`NetTimeProtocol::received` in `sync/src/net_time_checker.rs` contains a direction check (`is_inbound()`) that logs a warning when a time message arrives from an inbound peer, but **does not return early**. Processing continues unconditionally, allowing any unprivileged peer that connects to the node to inject arbitrary time-offset samples into the `NetTimeChecker` pool.

---

### Finding Description

The `NetTimeProtocol` implements a two-sided handshake:

- **`connected`** (lines 112–124): When an *inbound* peer connects, the node sends its own local timestamp to that peer. This is the "challenge" direction — the node initiates the time exchange toward peers that dialed in.
- **`received`** (lines 126–166): The node is supposed to collect time responses from *outbound* peers (peers it dialed out to), because those peers sent their timestamp when *they* saw the node as inbound.

The `received` handler contains this guard:

```rust
if let Some(true) = nc.get_peer(peer_index).map(|peer| peer.is_inbound()) {
    info!(
        "Received a time message from a non-outbound peer {}",
        peer_index
    );
}
```

The condition fires when the sender is an inbound peer — exactly the unauthorized direction. However, there is **no `return` statement**. Execution falls through to:

```rust
let timestamp: u64 = match packed::TimeReader::from_slice(&data) ...
let offset: i64 = (i128::from(now) - i128::from(timestamp)) as i64;
let mut net_time_checker = self.checker.write();
net_time_checker.add_sample(offset);
if let Err(offset) = net_time_checker.check() {
    warn!("Please check your computer's local clock ...");
}
```

The attacker-supplied timestamp is parsed, an offset is computed, and `add_sample` inserts it into the shared `NetTimeChecker` pool — regardless of whether the peer was authorized to send it.

This is structurally identical to the Allora `OnRecvPacket` bug: a sender-identity check exists but is rendered inert because the guarded block only logs and never halts processing.

---

### Impact Explanation

`NetTimeChecker` maintains a sliding window of up to `MAX_SAMPLES = 11` offset samples and computes their median. `check()` returns an error when the median exceeds `TOLERANT_OFFSET = 7_200_000 ms` (2 hours), triggering a `warn!` to the operator:

> "Please check your computer's local clock … Incorrect time setting may cause unexpected errors."

An attacker who opens ≥ `MIN_SAMPLES = 5` connections (or cycles connections to fill the window) and sends timestamps crafted to produce offsets > 2 hours can:

1. **Persistently trigger false clock-drift warnings**, causing operators to believe their system clock is wrong and potentially misconfigure it.
2. **Suppress legitimate warnings** by injecting near-zero offsets when the node's clock is genuinely skewed, masking a real problem.
3. **Exhaust the sample window** with attacker-controlled data, evicting legitimate outbound-peer samples and making the checker useless.

Because the `warn!` message explicitly says "may cause unexpected errors," a targeted operator may act on it — adjusting their system clock in a direction that benefits the attacker (e.g., making the node's timestamps diverge from consensus).

---

### Likelihood Explanation

- Any unprivileged peer can dial into a CKB node's public P2P port.
- No special capability, key, or privilege is required.
- The `SupportProtocols::Time` protocol is registered and active on all full nodes.
- The attack requires only sending a well-formed `packed::Time` message over the established session — a single packet.
- Cycling connections to refill the 11-sample window is trivial and does not require Sybil-level resources.

---

### Recommendation

Add an early return when the sender is an inbound peer, mirroring the intent already expressed by the log message:

```rust
async fn received(
    &mut self,
    nc: Arc<dyn CKBProtocolContext + Sync>,
    peer_index: PeerIndex,
    data: Bytes,
) {
    // Only accept time messages from outbound peers.
    // We send our time to inbound peers; they are not expected to reply.
    if nc.get_peer(peer_index).map(|peer| peer.is_inbound()).unwrap_or(false) {
        info!(
            "Ignoring time message from inbound peer {}; not an authorized sender",
            peer_index
        );
        return; // ← missing in current code
    }
    // ... rest of processing
}
```

---

### Proof of Concept

1. Attacker dials the target CKB node on its P2P listen address and negotiates `SupportProtocols::Time`.
2. The node's `connected` handler fires; because the attacker is inbound, the node sends its own timestamp to the attacker.
3. The attacker ignores the received timestamp and instead sends a crafted `packed::Time` message with `timestamp = 0` (producing an offset of ~current Unix time in ms, far exceeding `TOLERANT_OFFSET = 7_200_000`).
4. `received` fires: the `is_inbound()` branch logs a message but **does not return**.
5. `add_sample(offset)` inserts the attacker-controlled offset into the checker.
6. After 5 such samples (from 5 connections or repeated reconnections), `check()` returns `Err(offset)` and the node emits the clock-drift warning.
7. The attacker can maintain this state indefinitely by keeping connections open or cycling them faster than legitimate outbound peers can contribute samples.

Relevant code locations: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** sync/src/net_time_checker.rs (L11-13)
```rust
pub(crate) const TOLERANT_OFFSET: u64 = 7_200_000;
const MIN_SAMPLES: usize = 5;
const MAX_SAMPLES: usize = 11;
```

**File:** sync/src/net_time_checker.rs (L35-40)
```rust
    pub fn add_sample(&mut self, offset: i64) {
        self.samples.push_back(offset);
        if self.samples.len() > self.max_samples {
            self.samples.pop_front();
        }
    }
```

**File:** sync/src/net_time_checker.rs (L58-67)
```rust
    pub fn check(&self) -> Result<(), i64> {
        let network_offset = match self.median_offset() {
            Some(offset) => offset,
            None => return Ok(()),
        };
        if network_offset.unsigned_abs() > self.tolerant_offset {
            return Err(network_offset);
        }
        Ok(())
    }
```

**File:** sync/src/net_time_checker.rs (L112-123)
```rust
    async fn connected(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        _version: &str,
    ) {
        // send local time to inbound peers
        if let Some(true) = nc.get_peer(peer_index).map(|peer| peer.is_inbound()) {
            let now = ckb_systemtime::unix_time_as_millis();
            let time = packed::Time::new_builder().timestamp(now).build();
            let _status = async_send_message_to(&nc, peer_index, &time).await;
        }
```

**File:** sync/src/net_time_checker.rs (L126-166)
```rust
    async fn received(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        data: Bytes,
    ) {
        if let Some(true) = nc.get_peer(peer_index).map(|peer| peer.is_inbound()) {
            info!(
                "Received a time message from a non-outbound peer {}",
                peer_index
            );
        }

        let timestamp: u64 = match packed::TimeReader::from_slice(&data)
            .map(|time| time.timestamp().into())
            .ok()
        {
            Some(timestamp) => timestamp,
            None => {
                info!("Received a malformed message from peer {}", peer_index);
                nc.ban_peer(
                    peer_index,
                    BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };

        let now: u64 = ckb_systemtime::unix_time_as_millis();
        let offset: i64 = (i128::from(now) - i128::from(timestamp)) as i64;
        let mut net_time_checker = self.checker.write();
        debug!("New net time offset sample {}ms", offset);
        net_time_checker.add_sample(offset);
        if let Err(offset) = net_time_checker.check() {
            warn!(
                "Please check your computer's local clock ({}ms offset from network peers). Incorrect time setting may cause unexpected errors.",
                offset
            );
        }
    }
```
