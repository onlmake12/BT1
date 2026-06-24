Audit Report

## Title
Unbounded `pending_delivered` HashMap Growth via Unique `from` PeerID Flood — (`network/src/protocols/hole_punching/component/connection_request.rs`, `network/src/protocols/hole_punching/mod.rs`)

## Summary
An unprivileged remote peer can cause unbounded heap growth in the `HolePunching` protocol by sending `ConnectionRequest` messages addressed to the target node's own PeerID (`to` = self), each with a distinct random `from` PeerID. The `forward_rate_limiter` is trivially bypassed because it is keyed on `(from, to, item_id)` — unique `from` values each get a fresh bucket. The only real throttle is the per-session `rate_limiter` at 30 req/sec, but `pending_delivered` has no size cap and is only pruned every 5 minutes. With multiple simultaneous connections, memory growth scales linearly and can exhaust available RAM.

## Finding Description

**Entry point**: Any peer connected via P2P sends `HolePunchingMessage::ConnectionRequest` messages.

**Step 1 — Per-session rate limiter** (`mod.rs` lines 95–107):
The outer `rate_limiter` is keyed by `(session_id, msg.item_id())`. For `ConnectionRequest`, `item_id()` is always `0`. This allows at most 30 messages/second per session. [1](#0-0) 

**Step 2 — `forward_rate_limiter` bypass** (`connection_request.rs` lines 132–143):
The `forward_rate_limiter` is keyed by `(content.from, content.to, msg_item_id)`. With a unique `from` PeerID per message, each message gets its own fresh rate-limiter bucket (quota: 1/sec per key). Every message passes this check. [2](#0-1) 

**Step 3 — `respond_delivered` inserts without size cap** (`connection_request.rs` lines 161–237):
When `self_peer_id == &content.to`, `respond_delivered` is called. The only deduplication guard checks whether the same `from_peer_id` was seen within `HOLE_PUNCHING_INTERVAL` (2 min). With unique `from` values, this check is always missed, and a new entry is unconditionally inserted into `pending_delivered` after a successful `send_message_to`. [3](#0-2) [4](#0-3) 

**Step 4 — No size cap on `pending_delivered`**:
`pending_delivered` is a plain `HashMap<PeerId, PendingDeliveredInfo>` with no capacity limit or eviction policy. [5](#0-4) 

**Step 5 — Cleanup only every 5 minutes** (`mod.rs` lines 169–175):
The `notify` callback (fired every `CHECK_INTERVAL = 5 min`) is the only place `pending_delivered` is pruned. Entries inserted at the start of a window survive the full 5 minutes. [6](#0-5) [7](#0-6) 

**Secondary unbounded growth — `forward_rate_limiter` internal state**:
The `forward_rate_limiter` uses `governor::state::keyed::HashMapStateStore<(PeerId, PeerId, u32)>`. Each unique `(from, to, item_id)` triple creates a new entry in this store. `retain_recent()` is only called on peer disconnect, not periodically. During the attack, this store also grows without bound. [8](#0-7) [9](#0-8) 

**Prerequisite check at line 217 is trivially satisfied**:
The `remote_listens.is_empty()` guard filters attacker-supplied `listen_addrs` to TCP addresses with IPv4/IPv6. The attacker trivially satisfies this by including one address like `/ip4/1.2.3.4/tcp/1234/p2p/<from_peer_id>`. [10](#0-9) 

## Impact Explanation

**Per-connection memory growth** (single attacker session):
- Rate: 30 entries/sec × 300 sec (5-min window) = 9,000 entries
- Per entry: `PeerId` key (~39 bytes) + `Vec<Multiaddr>` (up to `ADDRS_COUNT_LIMIT = 24` addresses × ~30 bytes = ~720 bytes) + `u64` = ~800 bytes
- Single connection: ~7.2 MB per 5-minute window

With N simultaneous attacker connections: N × 9,000 entries. At 100 connections: ~720 MB. At 1,000 connections: ~7.2 GB — sufficient for OOM on typical node hardware.

The `forward_rate_limiter` internal state adds a compounding factor: 9,000 entries per connection per window, each holding a `(PeerId, PeerId, u32)` key (~82 bytes) plus governor rate state, never pruned until disconnect.

This matches the allowed CKB bounty impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

- The target node's PeerID is publicly discoverable via the identify/discovery protocols — no privileged access required.
- The attacker needs only standard P2P connections and the ability to craft `ConnectionRequest` messages with one valid TCP `listen_addr` (required to pass the `remote_listens.is_empty()` guard).
- The `forward_rate_limiter` bypass is trivial: generate a new random `PeerId` per message.
- The per-session rate limit (30/sec) is the only real constraint, and it is per-connection — opening more connections linearly amplifies the attack.
- No PoW, no privileged role, no leaked keys required.

## Recommendation

1. **Cap `pending_delivered`**: Enforce a hard maximum size (e.g., 1,024 entries). On overflow, reject new insertions or evict the oldest entry (LRU).
2. **Periodic `forward_rate_limiter` pruning**: Call `forward_rate_limiter.retain_recent()` inside the `notify` callback (every 5 minutes), not only on disconnect.
3. **Bind `pending_delivered` inserts to authenticated sessions**: Only insert into `pending_delivered` when the `from` PeerID corresponds to a currently-connected peer, preventing phantom PeerID injection.
4. **Reduce `CHECK_INTERVAL`**: A 5-minute cleanup window is too long given the 30/sec insertion rate. Reduce to 30–60 seconds.

## Proof of Concept

```
1. Discover target node's PeerID via identify protocol.
2. Open K TCP connections to the target node (K = desired amplification factor).
3. On each connection, in a loop at 30 msg/sec:
   a. Generate a fresh random PeerId (e.g., random 39-byte multihash).
   b. Craft a ConnectionRequest with:
      - from = fresh_random_peer_id
      - to   = target_node_peer_id
      - listen_addrs = [/ip4/1.2.3.4/tcp/1234/p2p/<fresh_random_peer_id>]  (1 valid TCP addr)
      - max_hops = 1
      - route = []
   c. Send over the HolePunching protocol stream.
4. After 5 minutes, observe pending_delivered.len() ≈ K × 9,000.
5. Measure RSS growth: ~800 bytes × K × 9,000.
   At K=100: ~720 MB. At K=1000: ~7.2 GB → OOM.
```

The `forward_rate_limiter` check passes for every message (each `from` is unique, so each gets a fresh 1/sec bucket). The `respond_delivered` deduplication check (lines 161–167) is bypassed because no two messages share the same `from`. The `remote_listens.is_empty()` guard (line 217) is satisfied by the single valid TCP address. Each message that reaches line 237 inserts unconditionally into the unbounded map.

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L25-28)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L31-35)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;
```

**File:** network/src/protocols/hole_punching/mod.rs (L44-44)
```rust
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
```

**File:** network/src/protocols/hole_punching/mod.rs (L66-69)
```rust
    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.rate_limiter.retain_recent();
        self.forward_rate_limiter.retain_recent();
        debug!("HolePunching.disconnected session={}", context.session.id);
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

**File:** network/src/protocols/hole_punching/mod.rs (L169-175)
```rust
    async fn notify(&mut self, context: &mut ProtocolContext, _token: u64) {
        let status = self.network_state.connection_status();

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L196-219)
```rust
        let remote_listens: Vec<Multiaddr> = remote_listens
            .into_iter()
            .filter_map(|addr| match find_type(&addr) {
                TransportType::Memory
                | TransportType::Onion
                | TransportType::Ws
                | TransportType::Wss
                | TransportType::Tls => None,
                TransportType::Tcp => {
                    if addr
                        .iter()
                        .any(|p| matches!(p, Protocol::Ip4(_) | Protocol::Ip6(_)))
                    {
                        Some(addr)
                    } else {
                        None
                    }
                }
            })
            .collect();

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
