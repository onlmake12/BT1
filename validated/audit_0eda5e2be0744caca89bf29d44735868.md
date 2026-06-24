Audit Report

## Title
Unbounded Memory Growth in `HolePunching` `forward_rate_limiter` via Attacker-Controlled PeerIds - (File: `network/src/protocols/hole_punching/mod.rs`)

## Summary

The `HolePunching` protocol's `forward_rate_limiter` is backed by an unbounded `HashMapStateStore` and keyed by `(content.from, content.to, msg_item_id)` values deserialized directly from the message payload. Because `retain_recent()` is only invoked on peer disconnect and never during the periodic `notify()` handler, a single connected peer can continuously insert new entries into the limiter's backing `HashMap` by sending messages with unique attacker-chosen `from`/`to` PeerId pairs, growing node memory without bound until OOM.

## Finding Description

`HolePunching` declares two rate limiters backed by `governor::state::keyed::HashMapStateStore`:

```rust
// network/src/protocols/hole_punching/mod.rs L31-46
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;
...
rate_limiter: RateLimiter<(PeerIndex, u32)>,
forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```

The outer `rate_limiter` is keyed by `(session_id, item_id)` — bounded by the number of active sessions. The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)`, where `from` and `to` are parsed directly from the message body in all three message processors:

- `connection_request.rs` L36–40: `from` and `to` parsed from raw bytes via `PeerId::from_bytes`
- `connection_request_delivered.rs` L38–42: same
- `connection_sync.rs` L42–46: same

Each call to `forward_rate_limiter.check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))` (confirmed at `connection_request.rs` L132–135, `connection_request_delivered.rs` L134–137, `connection_sync.rs` L85–88) inserts a new entry into the `HashMapStateStore` for every previously-unseen `(from, to, item_id)` triple.

The outer `rate_limiter` allows 30 messages/sec per `(session_id, item_id)` (L95–107 of `mod.rs`). This check passes before the `forward_rate_limiter` check, and it is keyed by session — not by `from`/`to`. An attacker using a fresh unique `(from, to)` pair for every message will always pass the `forward_rate_limiter` check (quota is 1/sec per key, but each new pair is a new key), inserting a new entry each time.

Cleanup of the `forward_rate_limiter` occurs only in `disconnected()`:

```rust
// mod.rs L66-70
async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
    self.rate_limiter.retain_recent();
    self.forward_rate_limiter.retain_recent();
    ...
}
```

The `notify()` handler (fires every 5 minutes, L169–175) prunes `pending_delivered` and `inflight_requests` by timestamp but **never calls `retain_recent()` on either rate limiter**. As long as the attacker maintains the connection, the `HashMapStateStore` grows indefinitely.

## Impact Explanation

This matches the allowed CKB bounty impact: **"Vulnerabilities which could easily crash a CKB node" — High (10001–15000 points)**.

At 30 messages/sec per message type across 3 message types, an attacker inserts up to 90 new entries/second. Each entry holds two `PeerId` values (~39 bytes each), a `u32`, and `HashMap` overhead (~150–200 bytes total). Over a sustained 24-hour connection: 90 × 86,400 = ~7.8M entries ≈ 1.2–1.5 GB of heap growth from a single peer. This exhausts available memory, triggering OOM conditions that crash or severely degrade the node, preventing block processing, transaction relay, and RPC service.

## Likelihood Explanation

Any unprivileged peer that can establish a TCP connection to a CKB node with `HolePunching` enabled can trigger this. No tokens, keys, or special privileges are required. The attack rate (30 msg/sec) is well within normal network capacity. The attacker can maintain the connection indefinitely, and the node has no periodic cleanup mechanism to bound the limiter's memory growth. The attack is fully automated and repeatable.

## Recommendation

1. **Add `retain_recent()` calls in `notify()`**: The `notify()` handler already fires every 5 minutes (`CHECK_INTERVAL`). Add `self.rate_limiter.retain_recent()` and `self.forward_rate_limiter.retain_recent()` inside it to periodically evict stale entries.
2. **Validate `from` against the sending session**: Before calling `forward_rate_limiter.check_key()`, verify that `content.from` matches the actual `PeerId` of the sending session. Messages where `from` does not match the session's peer ID should be rejected (or banned) before reaching the rate limiter, eliminating the ability to inject arbitrary keys.
3. **Use a bounded state store**: Replace `HashMapStateStore` for `forward_rate_limiter` with a capacity-bounded LRU-backed store to cap worst-case memory usage regardless of cleanup timing.

## Proof of Concept

1. Attacker establishes a single P2P connection to a target CKB node with `HolePunching` enabled.
2. Attacker sends `ConnectionRequest` messages at 30/sec. Each message contains a freshly generated random `from` PeerId and `to` PeerId (arbitrary valid byte strings).
3. Each message passes the outer `rate_limiter` check (keyed by session, L95–107 of `mod.rs`) and reaches `forward_rate_limiter.check_key(&(from, to, item_id))` (L132–135 of `connection_request.rs`), inserting a new entry because the `(from, to)` pair has never been seen before.
4. The `notify()` handler fires every 5 minutes but never calls `retain_recent()` on `forward_rate_limiter` (L169–175 of `mod.rs`), so no entries are ever evicted during the connection.
5. After 24 hours: ~2.6M entries from `ConnectionRequest` alone (plus entries from `ConnectionRequestDelivered` and `ConnectionSync`), consuming ~500MB–1.5GB of heap.
6. Node OOM-kills or becomes unresponsive; all users of that node lose access to transaction submission, block relay, and RPC.