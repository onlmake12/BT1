Audit Report

## Title
Unbounded `forward_rate_limiter` HashMapStateStore Growth via Attacker-Controlled `(from, to)` PeerIds — (`network/src/protocols/hole_punching/mod.rs`)

## Summary
The `HolePunching` protocol handler maintains a `forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>` backed by `governor::state::keyed::HashMapStateStore`. The key includes `content.from` and `content.to`, both fully attacker-controlled fields from the message body. The `retain_recent()` cleanup is called only in `disconnected()`, never in `notify()`. An attacker who maintains a persistent session and sends `ConnectionRequest` messages with unique `(from, to)` pairs at the outer rate limit (30/sec) causes the internal HashMap to grow without bound, exhausting heap memory and crashing the node.

## Finding Description
**`forward_rate_limiter` type and key** (lines 31–35, 46): The limiter is `RateLimiter<(PeerId, PeerId, u32)>` backed by `HashMapStateStore`. In `ConnectionRequestProcess::execute()` (lines 132–143 of `connection_request.rs`), the key is `(content.from.clone(), content.to.clone(), self.msg_item_id)` where `content.from` and `content.to` are parsed directly from the attacker-supplied message body (lines 36–40).

**`retain_recent()` only on disconnect** (lines 66–70 of `mod.rs`): Both `self.rate_limiter.retain_recent()` and `self.forward_rate_limiter.retain_recent()` are called only in `disconnected()`. The `notify()` handler (lines 169–175) cleans up `pending_delivered` and `inflight_requests` but never calls `retain_recent()` on either rate limiter.

**Outer rate limiter does not prevent the attack** (lines 95–107): The outer limiter is keyed by `(session_id, msg.item_id())` at 30/sec. This limits message throughput per session but does not prevent each message from carrying a unique `(from, to)` pair, inserting a new entry into `forward_rate_limiter` on every call.

**Route self-check bypass** (lines 127–130 of `connection_request.rs`): The `content.route.contains(self_peer_id)` check is bypassed by sending an empty `route` field, which is valid per the schema.

**First `check_key()` always succeeds for new keys**: With a quota of 1/sec, the first `check_key()` call for any previously unseen `(from, to, item_id)` triple always succeeds and inserts a new entry. Since the attacker never reuses a pair, every message passes the rate limiter and inserts a new entry.

**Single shared instance**: `HolePunching` is one struct per node (not per session), so all sessions write into the same `forward_rate_limiter`. Multiple attacker sessions multiply the growth rate proportionally.

## Impact Explanation
This is a **High** severity vulnerability matching "Vulnerabilities which could easily crash a CKB node." At 30 entries/sec from a single persistent session, the `HashMapStateStore` grows at ~90–100 bytes/entry. After hours of sustained attack (or minutes with multiple sessions), heap memory is exhausted and the node process crashes. No privileged access is required — only a standard P2P connection with `HolePunching` enabled.

## Likelihood Explanation
The attacker needs only a standard P2P connection to a node with `HolePunching` registered (the default configuration). No proof-of-work, keys, or privileges are required. The attack is trivially sustained by never disconnecting. The outer rate limiter (30/sec) merely sets the growth rate rather than preventing it. The route self-check is bypassed with an empty `route` field. The attack is fully repeatable and scales linearly with the number of attacker-controlled sessions.

## Recommendation
Call `self.forward_rate_limiter.retain_recent()` (and `self.rate_limiter.retain_recent()`) inside the `notify()` handler, which already fires every 5 minutes via `CHECK_INTERVAL`. This bounds the map size to entries active within the last rate-limit window regardless of whether peers disconnect:

```rust
async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
    // existing cleanup ...
    self.rate_limiter.retain_recent();
    self.forward_rate_limiter.retain_recent();
    // ...
}
```

## Proof of Concept
1. Connect to a target CKB node with `HolePunching` enabled.
2. In a loop at ≤30 messages/sec (to stay under the outer limiter):
   - Generate a fresh random `PeerId` for `from` and a fresh random `PeerId` for `to`.
   - Construct a valid `ConnectionRequest` molecule message: `from=random_A`, `to=random_B`, `max_hops=1`, `route=[]` (empty, bypasses self-peer-id check), `listen_addrs=[one valid TCP multiaddr]`.
   - Send over the `HolePunching` protocol stream.
3. Never disconnect.
4. After `T` seconds, the `forward_rate_limiter` `HashMapStateStore` contains `30*T` entries. Assert map size grows linearly with `T` and is not bounded by the number of active sessions.
5. After sufficient time (hours at 30/sec, or minutes with many concurrent sessions), the node process OOMs and crashes.