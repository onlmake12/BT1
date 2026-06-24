Audit Report

## Title
Unbounded Heap Growth via Unique `from` Peer IDs Bypassing `forward_rate_limiter` and `pending_delivered` Deduplication — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary

An unprivileged remote peer can cause unbounded heap growth in the victim node's `HolePunching` protocol state by sending `ConnectionRequest` messages with a distinct random `from` peer ID per message. Both the `forward_rate_limiter` (keyed on `(from, to, item_id)`) and the `pending_delivered` cooldown guard (keyed on `from_peer_id`) are bypassed entirely because each unique `from` creates a fresh, never-rate-limited bucket. The `forward_rate_limiter`'s internal `HashMapStateStore` is never cleaned during an active session, making it a truly unbounded growth vector. Combined across multiple inbound sessions, this can exhaust node memory and cause an OOM crash.

## Finding Description

**Outer rate limiter** (`mod.rs` L95–107) is keyed by `(session_id, item_id)` and correctly caps each session to 30 `ConnectionRequest` messages per second. This guard is not bypassed and sets the maximum insertion rate.

**`forward_rate_limiter`** (`connection_request.rs` L132–143) is keyed by `(content.from, content.to, self.msg_item_id)`. Because `content.from` is fully attacker-controlled, each message with a fresh random `from` creates a brand-new bucket in the `HashMapStateStore`. The 1-req/sec quota is never reached for any individual key, so the check always passes. Critically, `forward_rate_limiter.retain_recent()` is only called in `disconnected()` (`mod.rs` L66–68), never in `notify()`. For a long-lived connection, the `HashMapStateStore` accumulates entries at 30/sec indefinitely with no eviction.

**`pending_delivered` deduplication guard** (`connection_request.rs` L161–167) is keyed by `from_peer_id`. With a unique `from` per message, `pending_delivered.get(&from_peer_id)` always returns `None`, so the 2-minute `HOLE_PUNCHING_INTERVAL` cooldown is never triggered. The unconditional insertion at `connection_request.rs` L234–237 fires on every successful send.

**`pending_delivered` cleanup** (`mod.rs` L173–174) only runs in `notify()` every 5 minutes (`CHECK_INTERVAL`, `mod.rs` L25), retaining entries where `(now - t) < TIMEOUT` (5 minutes, `mod.rs` L28). This means up to a full 5-minute window of insertions accumulates before any eviction.

The exploit path for `pending_delivered` requires `to=victim_peer_id` (line 145 of `connection_request.rs`). The `forward_rate_limiter` growth requires only any valid `ConnectionRequest` with unique `from`, regardless of `to`.

## Impact Explanation

**`pending_delivered` (per 5-minute window, all sessions):**
- Single session: 30/sec × 300 sec = 9,000 entries. Each entry: `PeerId` key (~39 bytes) + `Vec<Multiaddr>` (~50–100 bytes) + `u64` ≈ ~1–2 MB per session per window.
- With 125 inbound sessions: ~125–250 MB per 5-minute window.

**`forward_rate_limiter` (unbounded for long-lived connections):**
- Single session: 30 entries/sec, never cleaned. Over 1 hour: ~108,000 entries × ~100 bytes ≈ ~10 MB/session/hr.
- With 125 sessions: ~1.25 GB/hr from rate-limiter state alone.
- Over 24 hours: ~30 GB, causing OOM crash.

This matches the **High** impact: *"Vulnerabilities which could easily crash a CKB node."*

## Likelihood Explanation

The attack requires only:
1. A single TCP connection to the victim (no authentication, no PoW, no key material).
2. `ConnectionRequest` messages with `to=victim_peer_id` (publicly known from the P2P network), a fresh random valid multihash `from` per message, and at least one valid TCP IPv4/IPv6 `listen_addr`.
3. Sustaining the connection for hours.

All conditions are trivially achievable by any remote peer. The `from` field undergoes only multihash validity parsing (`connection_request.rs` L36–38); no cryptographic ownership proof is required.

## Recommendation

1. **Key `forward_rate_limiter` on `(session_id, to, item_id)`** instead of `(from, to, item_id)`. The `from` field is fully attacker-controlled and unbounded; `session_id` is bounded by the number of active connections.
2. **Call `self.forward_rate_limiter.retain_recent()` inside `notify()`** in addition to `disconnected()`, so stale entries are evicted every 5 minutes regardless of connection lifetime.
3. **Bound `pending_delivered` by size**, not only by time. Add a `MAX_PENDING_DELIVERED` constant (e.g., 1,024) and reject insertions when the map is full, or use an LRU eviction policy.
4. **Add a per-session cap on `pending_delivered` insertions**, since the current design allows a single session to fill the shared map at the full 30/sec rate.

## Proof of Concept

```rust
// Minimal unit test sketch
let mut protocol = HolePunching::new(network_state.clone());
let session_id = PeerIndex::new(1);
let victim_peer_id = network_state.local_peer_id().clone();

for _ in 0..9000 {
    let from = PeerId::random(); // unique per iteration — bypasses both guards
    let msg = build_connection_request(
        from,
        victim_peer_id.clone(),
        vec!["/ip4/1.2.3.4/tcp/8115".parse().unwrap()],
    );
    // outer rate_limiter: 30/sec per (session_id, item_id) — throttles to 30/sec but does not prevent growth
    // forward_rate_limiter: (from, to, 0) — always a new key, always passes, always inserts
    // pending_delivered: get(&from) == None — always passes, always inserts
    ConnectionRequestProcess::new(msg, &mut protocol, session_id, &control, 0)
        .execute()
        .await;
}

// After 5 minutes (300 sec × 30/sec):
assert_eq!(protocol.pending_delivered.len(), 9000);
// forward_rate_limiter internal HashMapStateStore also holds 9000 entries, never evicted
```

Manual verification: connect to a live node, stream `ConnectionRequest` messages at 30/sec with `to=<victim_peer_id>` and a fresh random `from` each time. Monitor RSS growth; `forward_rate_limiter` state grows linearly with no bound until disconnect.