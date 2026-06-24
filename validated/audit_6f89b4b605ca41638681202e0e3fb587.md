Audit Report

## Title
Forward Rate Limiter Bypass via Attacker-Controlled `content.from`/`content.to` Enables Gossip Amplification and Unbounded Memory Growth - (File: `network/src/protocols/hole_punching/component/connection_request.rs`, `connection_request_delivered.rs`, `connection_sync.rs`)

## Summary

The `forward_rate_limiter` in the HolePunching protocol is keyed by `(content.from, content.to, msg_item_id)`, values read directly from the attacker-controlled message payload. An attacker can bypass this limiter entirely by rotating `content.from` across successive messages, causing each message to receive its own fresh rate-limit bucket. The only real constraint becomes the outer 30 req/sec cap, and all 30 messages per second are forwarded via gossip to `sqrt(N)` peers per hop, creating network-wide amplification. Additionally, the `forward_rate_limiter`'s `HashMapStateStore` accumulates one entry per unique `(from, to)` pair and is never cleaned up during a live connection.

## Finding Description

**Root cause — attacker-controlled rate limiter key:**

In `mod.rs` L45-46, two rate limiters are declared:
```rust
rate_limiter: RateLimiter<(PeerIndex, u32)>,
forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```

The outer `rate_limiter` is correctly keyed by the unforgeable `session_id` (`mod.rs` L95-97):
```rust
if self.rate_limiter.check_key(&(session_id, msg.item_id())).is_err() { ... }
```

The inner `forward_rate_limiter` is keyed by payload-derived values. In `connection_request.rs` L132-135:
```rust
self.protocol.forward_rate_limiter
    .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
```

`content.from` and `content.to` are parsed directly from the wire message (`connection_request.rs` L36-40):
```rust
let from = PeerId::from_bytes(value.from().raw_data().to_vec())...;
let to   = PeerId::from_bytes(value.to().raw_data().to_vec())...;
```

The identical pattern appears in `connection_request_delivered.rs` L134-137 and `connection_sync.rs` L85-88.

**Exploit flow:**

1. Attacker connects and negotiates the HolePunching protocol (no special privileges required).
2. Attacker sends up to 30 `ConnectionRequest` messages/sec (outer cap), each with a freshly generated random `content.from` PeerId.
3. For each message, `forward_rate_limiter.check_key(&(content.from, content.to, item_id))` sees a brand-new key and passes immediately — the 1 req/sec per-pair limit is never triggered.
4. The node forwards all 30 messages/sec. Each forwarded message is gossiped to `sqrt(N)` peers (`connection_request.rs` L280-298). Those peers forward to their own `sqrt(N)` peers, up to `MAX_HOPS = 6` hops (`mod.rs` L23).
5. Total network traffic scales as `30 × sqrt(N)^6 = 30 × N^3` messages/sec from a single attacker session.

**Why existing checks are insufficient:**

The outer `rate_limiter` (30 req/sec per session) is the only real constraint. The `forward_rate_limiter` — whose stated purpose is "the same group of from/to should not be received by the same node more than 1 times within one second" (`mod.rs` L254-255) — provides zero protection because its key is entirely attacker-controlled.

**Unbounded memory growth:**

`governor::RateLimiter` with `HashMapStateStore` allocates one map entry per unique key. With 30 unique `(from, to)` pairs/sec, the store grows at 30 entries/sec. `retain_recent()` is called only on disconnect (`mod.rs` L67-68):
```rust
async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
    self.rate_limiter.retain_recent();
    self.forward_rate_limiter.retain_recent();
```

The `notify` handler (`mod.rs` L169-175) cleans `pending_delivered` and `inflight_requests` every 5 minutes but does **not** clean `forward_rate_limiter`. A long-lived connection accumulates entries indefinitely (~108,000 entries/hour, growing without bound).

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

A single connected peer can drive 30 forwarded messages/sec through any intermediate node. Each message is gossiped to `sqrt(N)` downstream peers, who repeat the process up to 6 hops. The amplification factor is `N^3` in the worst case. With a modest network of 100 connected peers per node, a single attacker session generates on the order of millions of forwarded messages per second across the network. The secondary unbounded memory growth in `forward_rate_limiter`'s `HashMapStateStore` can additionally exhaust node memory over time, potentially crashing individual nodes.

## Likelihood Explanation

Any peer that establishes a single HolePunching protocol session can exploit this. No special privileges, cryptographic keys, or majority hashpower are required. The attacker simply calls `PeerId::random()` for each message. The outer 30 req/sec cap is the only real constraint, making this trivially and continuously exploitable for the duration of any connection.

## Recommendation

Key the `forward_rate_limiter` by the unforgeable network peer identity (`PeerIndex` / `session_id`) rather than by attacker-controlled message fields, matching the pattern already used by the outer `rate_limiter`:

```rust
// Current (bypassable): keyed by attacker-controlled payload
self.forward_rate_limiter.check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))

// Fix: keyed by unforgeable session identity
self.forward_rate_limiter.check_key(&(self.peer, self.msg_item_id))
```

If the semantic intent is to deduplicate forwarding of the same logical `(from, to)` request across *different* peers, replace the unbounded `HashMapStateStore`-backed rate limiter with a fixed-capacity LRU set of recently seen `(from, to, item_id)` tuples. Additionally, call `forward_rate_limiter.retain_recent()` periodically in the `notify` handler (every 5 minutes alongside the existing `pending_delivered` cleanup) to bound memory growth even before the fix is applied.

## Proof of Concept

1. Connect to a CKB node and negotiate the `HolePunching` protocol.
2. In a loop at 30 msg/sec (the outer cap), send `ConnectionRequest` messages where:
   - `content.from` = `PeerId::random()` (fresh random value each iteration)
   - `content.to` = any valid target PeerId
   - `listen_addrs` = a valid non-empty address list
3. Observe that `forward_rate_limiter.check_key(...)` passes for every message (each sees a new bucket).
4. Observe that the node forwards all 30 messages/sec to `sqrt(N)` downstream peers.
5. After N seconds, inspect the `forward_rate_limiter`'s internal state: it contains N×30 entries, growing without bound.
6. A fuzz test can confirm this by asserting that after sending K messages with K distinct `from` PeerIds, the forward rate limiter has never returned `Err` despite K >> 1.