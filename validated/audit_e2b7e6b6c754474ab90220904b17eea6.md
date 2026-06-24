Audit Report

## Title
Unbounded `pending_delivered` HashMap Growth via Spoofed `from` PeerIds in `ConnectionRequest` — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary
An unprivileged remote peer can exhaust heap memory on a victim CKB node by flooding it with `ConnectionRequest` P2P messages, each carrying a unique spoofed `from` PeerId and the victim's own peer ID as `to`. The session-level rate limiter caps throughput at 30 messages/second per session but does not bound the total number of unique entries inserted into `pending_delivered`. With no size cap on the map and cleanup only occurring every 5 minutes in `notify()`, an attacker with 117 inbound connections can accumulate over 1 million entries (~860 MB) before the first pruning cycle, causing OOM on nodes with 1–2 GB RAM.

## Finding Description

**Entry point:** Any peer connected to the victim sends `ConnectionRequest` messages over the `HolePunching` P2P protocol.

**Guard 1 — session-level rate limiter** (`mod.rs` lines 95–107):
Keyed by `(session_id, msg.item_id())`. For `ConnectionRequest`, `msg_item_id = 0`, so the key is `(session_id, 0)` — one key per session, capped at 30 messages/second. This is the only real throttle and does not bound total unique insertions across sessions. [1](#0-0) 

**Guard 2 — `forward_rate_limiter`** (`connection_request.rs` lines 132–143):
Keyed by `(content.from, content.to, msg_item_id)`. With a unique `from` PeerId per message, every message creates a new key and passes unconditionally. This guard provides zero protection against spoofed `from` IDs. [2](#0-1) 

**Guard 3 — dedup check in `respond_delivered`** (`connection_request.rs` lines 161–167):
Only suppresses re-insertion if the same `from` PeerId was seen within `HOLE_PUNCHING_INTERVAL` (2 minutes). With unique `from` IDs, every message passes this check. [3](#0-2) 

**Unbounded insert** (`connection_request.rs` lines 234–237):
After `send_message_to` succeeds (which it will, since the attacker is the connected peer), a new entry `(from_peer_id → (remote_listens, now))` is unconditionally inserted with no size cap. [4](#0-3) 

**Cleanup only in `notify()`** (`mod.rs` lines 172–175):
`pending_delivered` is pruned by timestamp every `CHECK_INTERVAL = 5 minutes`. There is no maximum-size eviction between cleanup cycles. [5](#0-4) 

**Secondary unbounded structure — `forward_rate_limiter`** (`mod.rs` lines 45–46, 66–68):
This `HashMapStateStore`-backed rate limiter accumulates one entry per unique `(from, to, msg_item_id)` key. `retain_recent()` is only called on `disconnected()`. While the attacker maintains the connection, this map grows at the same rate as `pending_delivered` and is never pruned. [6](#0-5) [7](#0-6) 

**Precondition for `respond_delivered` path:** The message only reaches `respond_delivered` when `self_peer_id == &content.to` (line 145). The attacker sets `to` to the victim's peer ID (publicly discoverable via peer exchange or `get_peers` RPC) and must supply at least one valid TCP/IP4 or TCP/IP6 address in `listen_addrs` (line 217–219), both trivially satisfied. [8](#0-7) [9](#0-8) 

## Impact Explanation

**Impact class: High — Vulnerabilities which could easily crash a CKB node (10001–15000 points).**

Default CKB configuration allows up to 117 inbound connections (`max_peers = 125`, `max_outbound_peers = 8`). [10](#0-9) 

Growth rate: 30 entries/second × 117 sessions = 3,510 entries/second. Over 5 minutes (before `notify()` cleanup): ~1,053,000 entries. At ~817 bytes/entry (PeerId ~39 B + Vec<Multiaddr> up to 24 × ~30 B + timestamp 8 B + overhead ~50 B), `pending_delivered` alone reaches ~860 MB. The `forward_rate_limiter` adds ~100–200 MB. Combined peak of ~1 GB is sufficient to OOM nodes with 1–2 GB RAM. The attack is continuous — after each 5-minute cleanup cycle, the attacker immediately refills the map.

## Likelihood Explanation

- Attacker only needs standard P2P connections — no authentication, no PoW, no privileged role.
- The victim's peer ID is publicly discoverable via peer exchange or the `get_peers` RPC.
- The attacker supplies any valid routable TCP/IP4 or TCP/IP6 address in `listen_addrs` (e.g., `"/ip4/1.2.3.4/tcp/8115"`).
- The `from` PeerId field is not authenticated; any valid-format PeerId bytes are accepted by `PeerId::from_bytes` at parse time.
- The attack is fully automatable from a single machine with multiple connections.
- The `forward_rate_limiter` bypass via unique `from` IDs is a structural property of the keying scheme, not a configuration error.

## Recommendation

1. **Cap `pending_delivered` size**: Enforce a maximum entry count (e.g., `MAX_PENDING_DELIVERED = 1024`). When the cap is reached, reject new insertions or evict the oldest entry.
2. **Periodic `forward_rate_limiter` cleanup**: Call `self.forward_rate_limiter.retain_recent()` inside `notify()`, not only on `disconnected()`.
3. **Limit `from` PeerId insertions per session**: Track how many `pending_delivered` entries originated from each session and reject further insertions once a per-session cap is reached.
4. **Reduce `CHECK_INTERVAL`** or use a shorter `TIMEOUT` to bound the maximum map size between cleanup cycles.

## Proof of Concept

```rust
// Attacker holds 117 inbound connections to victim.
// On each connection, send at 30 msg/sec with unique random `from` PeerIds:
for i in 0..9000 {
    let rand_from = PeerId::random();
    let msg = ConnectionRequest {
        from: rand_from,
        to: victim_peer_id.clone(),   // publicly known
        max_hops: 6,
        listen_addrs: vec!["/ip4/1.2.3.4/tcp/8115".parse().unwrap()],
        route: vec![],
    };
    send_to_victim(msg);
    sleep(Duration::from_millis(33)); // stay within 30/sec session cap
}
// After 5 minutes with 117 connections:
// pending_delivered.len() ≈ 1,053,000  (~860 MB)
// forward_rate_limiter internal map ≈ 1,053,000 entries (~100-200 MB)
// Total heap: ~1 GB → OOM on 1-2 GB RAM nodes
```

A unit test asserting `pending_delivered.len() <= N` for any reasonable `N` would fail immediately after a burst of messages with distinct `from` PeerIds, as no size bound is enforced between `notify()` calls.

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L45-46)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```

**File:** network/src/protocols/hole_punching/mod.rs (L66-68)
```rust
    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.rate_limiter.retain_recent();
        self.forward_rate_limiter.retain_recent();
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

**File:** network/src/protocols/hole_punching/mod.rs (L172-175)
```rust
        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-148)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
        } else if content.max_hops == 0u8 {
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L161-167)
```rust
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
            }
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L217-219)
```rust
        if remote_listens.is_empty() {
            return StatusCode::Ignore.with_context("remote listen address is empty");
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** resource/ckb.toml (L94-95)
```text
max_peers = 125
max_outbound_peers = 8
```
