Audit Report

## Title
Unbounded `forward_rate_limiter` HashMap Growth via Attacker-Controlled `(from, to)` PeerIds Causes OOM — (`network/src/protocols/hole_punching/mod.rs`)

## Summary

`HolePunching::forward_rate_limiter` is a `governor::RateLimiter` backed by `HashMapStateStore<(PeerId, PeerId, u32)>`. Its keys are derived from attacker-controlled `from`/`to` fields in forwarded messages. `retain_recent()` is called only in `disconnected()`, never in the periodic `notify()` handler. A single long-lived session sending messages with unique `(from, to)` pairs causes the HashMap to grow without bound until the node OOMs.

## Finding Description

`forward_rate_limiter` is declared as `RateLimiter<(PeerId, PeerId, u32)>` backed by `HashMapStateStore` at `mod.rs` lines 31–46. All three message handlers — `ConnectionRequestProcess`, `ConnectionRequestDeliveredProcess`, and `ConnectionSyncProcess` — call `forward_rate_limiter.check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))` with attacker-controlled payload bytes. The `from`/`to` fields are only validated as structurally valid `PeerId` bytes; there is no check that they correspond to known or connected peers.

The outer `rate_limiter` (keyed by `(session_id, msg.item_id())`) limits to 30 messages/sec per `(session, type)` pair. This bounds throughput but not the total unique-key count in `forward_rate_limiter`. Since each unique `(from, to, item_id)` tuple gets its own fresh governor bucket, the rate limit is never triggered — every message both passes and inserts a new entry.

`retain_recent()` is called on both limiters only inside `disconnected()` (`mod.rs` lines 66–70). The `notify()` handler (`mod.rs` lines 169–175), which fires every `CHECK_INTERVAL = 5 minutes`, cleans `pending_delivered` and `inflight_requests` but never calls `retain_recent()` on either rate limiter. Stale entries therefore accumulate for the entire lifetime of the session.

## Impact Explanation

This is a **High** severity vulnerability matching: **"Vulnerabilities which could easily crash a CKB node."**

With 3 message types each allowing 30 unique keys/sec, an attacker can insert up to 90 new HashMap entries/sec from a single session. Each entry holds two `PeerId` values (~39 bytes each), a `u32`, and governor bucket state (~150–200 bytes total). This yields ~48 MB/hour, ~1.2 GB/day from a single attacker connection. Sustained over hours to days, the node process is OOM-killed, causing a full denial of service.

## Likelihood Explanation

The attacker requires only a single unprivileged P2P connection with the HolePunching protocol negotiated — no proof-of-work, no keys, no special role. The attack is trivially automatable: craft `ConnectionRequest`, `ConnectionRequestDelivered`, or `ConnectionSync` messages with random 32-byte `from`/`to` fields at ≤30 msg/sec and hold the session open indefinitely. The outer rate limiter throttles rate but does not bound total unique-key accumulation.

## Recommendation

Call `retain_recent()` on both rate limiters inside the `notify()` handler, which already fires every 5 minutes:

```rust
async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
    self.rate_limiter.retain_recent();           // add
    self.forward_rate_limiter.retain_recent();   // add
    // ... existing cleanup ...
}
```

This bounds the HashMap to entries active within the last rate-limit window, regardless of session duration.

## Proof of Concept

```python
# Single TCP session, send N ConnectionRequest messages
# each with a unique random (from_peer_id, to_peer_id)
import os, time
session = connect_to_ckb_node_hole_punching_protocol()
for _ in range(10_000_000):
    from_id = os.urandom(32)
    to_id   = os.urandom(32)
    msg = build_connection_request(from_id, to_id, max_hops=6, listen_addrs=[...])
    session.send(msg)
    time.sleep(1/30)  # stay within outer rate limit
# Monitor: node RSS grows ~150-200 bytes per iteration; no cleanup until disconnect
# Expected: node OOM-killed after sustained run (hours to days)
```

Verification steps:
1. Run a CKB node with HolePunching protocol enabled.
2. Connect a single peer session and send `ConnectionRequest` messages at 30/sec with unique random `from`/`to` byte pairs.
3. Monitor node RSS — it grows monotonically with no cleanup until the session disconnects.
4. Confirm `retain_recent()` is absent from `notify()` at `mod.rs` lines 169–175.
5. After applying the fix (adding `retain_recent()` calls to `notify()`), confirm RSS stabilizes.