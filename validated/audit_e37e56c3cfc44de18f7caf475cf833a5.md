### Title
Hole Punching `ConnectionRequest` Forwarding Allows Unbounded sqrt(N) Broadcast Amplification via Unique `to` PeerIds — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

An unprivileged connected peer can send up to 30 `ConnectionRequest` messages per second, each with a distinct `to` PeerId not present in the victim's peer registry. Each such message triggers an unconditional `filter_broadcast` to `sqrt(N)` peers. Because the `forward_rate_limiter` is keyed by `(from, to, item_id)`, each unique `to` value opens a fresh rate-limit bucket, making the limiter completely ineffective against this pattern. Each relay node repeats the same broadcast, creating a cascade bounded only by `MAX_HOPS = 6`.

---

### Finding Description

**Entrypoint — outer rate limiter (30 msg/s per session):**

In `HolePunching::received`, the first guard is:

```rust
if self.rate_limiter.check_key(&(session_id, msg.item_id())).is_err() { return; }
``` [1](#0-0) 

The key is `(PeerIndex, u32)` where `u32 = msg.item_id()` — always `0` for `ConnectionRequest`. This allows **30 messages per second** from a single session.

**Forward rate limiter — keyed by `(from, to, item_id)`:**

Inside `ConnectionRequestProcess::execute`, the second guard is:

```rust
if self.protocol.forward_rate_limiter
    .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
    .is_err()
{ return StatusCode::TooManyRequests; }
``` [2](#0-1) 

The rate limiter is configured at 1 request/second per key: [3](#0-2) 

**The bypass:** The attacker controls both `from` and `to` fields in the message payload. By using a fresh random `to` PeerId in each message, every message gets a brand-new bucket in the `HashMapStateStore`. The limiter never fires.

**The broadcast — sqrt(N) fan-out:**

When `to` is not found in the peer registry, `forward_message` executes:

```rust
let mut total = self.protocol.network_state
    .with_peer_registry(|p| p.peers().len())
    .isqrt();
self.p2p_control.filter_broadcast(
    TargetSession::Filter(Box::new(move |id| {
        if id == &sid { return false; }
        total = total.saturating_sub(1);
        total != 0
    })),
    proto_id, new_message,
).await
``` [4](#0-3) 

**The cascade:** Each of the `sqrt(N)` relay nodes receives the forwarded message. They run the same logic: `to` is unknown to them, their own `forward_rate_limiter` sees a new `(from, to, 0)` key (unique `to`), and they each broadcast to `sqrt(N)` more peers. This repeats for up to `MAX_HOPS = 6` hops. [5](#0-4) 

The `route` field prevents per-message loops but does not prevent the fan-out cascade — each node only checks whether it has already seen *this specific message*, not whether the `to` PeerId is being abused.

---

### Impact Explanation

For a network with N=100 peers (sqrt=10), a single attacker session sending 30 messages/second with unique `to` PeerIds causes:

| Hop | Messages generated |
|-----|--------------------|
| 1   | 30 × 10 = 300      |
| 2   | 300 × 10 = 3,000   |
| 3   | 3,000 × 10 = 30,000|
| ... | ...                |
| 6   | ~30,000,000        |

Total: ~33 million forwarded messages per second across the network from a single attacker peer. This congests the P2P layer and delays block/transaction propagation on all relay nodes.

---

### Likelihood Explanation

- Requires only a standard P2P connection — no privileges, no keys, no hashpower.
- The bypass is trivial: generate random `to` PeerIds (39-byte multihash values).
- The outer 30 msg/s cap is the only real constraint, and it is the amplification *source*, not a mitigation.
- Locally testable with a small CKB devnet.

---

### Recommendation

1. **Key the `forward_rate_limiter` by the sending session** (`PeerIndex`), not by `(from, to)`. This prevents a single peer from opening unlimited buckets.
2. **Add a global per-node cap** on total outbound hole-punching broadcasts per second, independent of the number of unique `(from, to)` pairs seen.
3. **Validate that `from` matches the sending session's PeerId**, preventing spoofed `from` fields from further multiplying the key space.
4. Consider **not broadcasting when `to` is unknown** unless the node has an active inflight request for that `to` PeerId.

---

### Proof of Concept

```python
# Pseudocode — connect to victim with N=100 peers
import random, time
from ckb_p2p import connect, build_connection_request

session = connect("victim_node:8115")
K = 100  # number of unique to PeerIds

for i in range(K):
    to_peer_id = random.randbytes(39)   # unique unknown PeerId each time
    from_peer_id = random.randbytes(39) # any valid PeerId
    msg = build_connection_request(
        from_=from_peer_id,
        to=to_peer_id,
        max_hops=6,
        listen_addrs=["127.0.0.1:8115"],
    )
    session.send(msg)
    # Each message passes forward_rate_limiter (new key) and triggers sqrt(100)=10 broadcasts
    # Expected: K * 10 = 1000 outbound messages from victim in < 1 second
    # At relay nodes: 1000 * 10 = 10,000 more, cascading to ~1,111,110 total
```

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L23-23)
```rust
pub(crate) const MAX_HOPS: u8 = 6;
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

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L273-305)
```rust
            None => {
                debug!(
                    "target peer {} is not found, broadcast the request to more peers",
                    to_peer_id
                );

                // Broadcast to a number of nodes equal to the square root of the total connection count using gossip.
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
