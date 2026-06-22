### Title
Unbounded `pending_delivered` HashMap Growth via Distinct `from` PeerIds in `ConnectionRequest` — (`network/src/protocols/hole_punching/mod.rs`)

### Summary

An unprivileged remote peer can exhaust the victim node's memory by sending a stream of `ConnectionRequest` messages addressed to the victim (`to == self_peer_id`) with a fresh, attacker-controlled `from` PeerId in each message. Because the per-`from` deduplication guard (`HOLE_PUNCHING_INTERVAL`) is keyed on `from`, each new `from` bypasses it and causes a new `(Vec<Multiaddr>, u64)` entry to be inserted into `pending_delivered`. The map has no size cap and is only cleaned up every 5 minutes.

### Finding Description

**Entry point — `received()` in `mod.rs`:**

The outer rate limiter is keyed by `(session_id, msg_item_id)`: [1](#0-0) 

This caps a single connection at 30 `ConnectionRequest` messages per second, but does not bound the total number of distinct `from` PeerIds that can be inserted.

**`execute()` in `connection_request.rs`:**

The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)`: [2](#0-1) 

With a fresh `from` PeerId per message, each message gets its own bucket — this limiter provides zero protection against the attack.

**`respond_delivered()` — the insertion site:**

The deduplication guard only fires when the same `from` PeerId is seen again within `HOLE_PUNCHING_INTERVAL`: [3](#0-2) 

A new `from` PeerId always passes this check. After filtering addresses to TCP/IPv4/IPv6 (which the attacker satisfies trivially), the entry is unconditionally inserted: [4](#0-3) 

**`pending_delivered` — no size cap:** [5](#0-4) 

**Cleanup — only every 5 minutes:** [6](#0-5) 

`CHECK_INTERVAL` is 5 minutes and `TIMEOUT` is also 5 minutes, so entries accumulate for the full window before any eviction. [7](#0-6) 

**Secondary unbounded structure — `forward_rate_limiter`:**

`retain_recent()` is only called on `disconnected`, never periodically: [8](#0-7) 

Each distinct `(from, to, msg_item_id)` triple permanently occupies a slot in the `HashMapStateStore` until the session closes, compounding the memory growth.

### Impact Explanation

**Memory math (conservative, single connection):**
- Rate: 30 insertions/sec (outer rate limiter)
- Window: 300 s (5-minute cleanup interval)
- Entries per connection: 9,000
- Per entry: `PeerId` key (~39 B) + `Vec<Multiaddr>` (up to 24 × ~60 B) + `u64` ≈ 1,500 B
- Memory per connection: ~13.5 MB

**With default max inbound connections** (`max_peers - max_outbound_peers = 125 - 8 = 117`): [9](#0-8) [10](#0-9) 

- Total entries: 117 × 9,000 = ~1,053,000
- Total memory: ~1.58 GB — sufficient to OOM a typical production node

The attack is sustained: as long as the attacker maintains connections and rotates `from` PeerIds, memory grows continuously until the OS kills the process.

### Likelihood Explanation

- No authentication or PoW required — any peer that can open a TCP connection can execute this.
- The `HolePunching` protocol is enabled by default in `support_protocols`: [11](#0-10) 
- The attacker needs only one or a few connections; even a single connection at 30/sec × 5 min = 9,000 entries × 1,500 B = ~13.5 MB per cycle, and the cycle resets every 5 minutes, so memory grows without bound as long as the connection is held.
- The attack is locally reproducible with a simple loop sending crafted `ConnectionRequest` messages.

### Recommendation

1. **Cap `pending_delivered` size**: Enforce a hard limit (e.g., 1,024 entries). Reject new insertions when the cap is reached, or evict the oldest entry (LRU).
2. **Cap `inflight_requests` size** similarly.
3. **Periodic `forward_rate_limiter` cleanup**: Call `retain_recent()` inside `notify()`, not only on `disconnected`, to prevent unbounded growth of the rate-limiter's internal `HashMapStateStore`.
4. **Per-session insertion budget**: Track how many `pending_delivered` entries originated from each session and evict them on disconnect.

### Proof of Concept

```
for i in 0..N:
    from_peer_id = generate_fresh_secp256k1_keypair().peer_id()
    listen_addrs = ["/ip4/1.2.3.4/tcp/1234"] * 24   # valid TCP/IPv4
    msg = ConnectionRequest {
        from: from_peer_id,
        to:   victim_peer_id,   # self_peer_id of target node
        max_hops: 6,
        route: [],
        listen_addrs: listen_addrs,
    }
    send_to_victim(msg)

# Assert: victim's pending_delivered.len() grows linearly with N
# Assert: victim RSS grows by ~1,500 * N bytes
```

Each iteration bypasses the `HOLE_PUNCHING_INTERVAL` guard (new key), bypasses the `forward_rate_limiter` (new `(from, to)` pair), and is only throttled by the 30 req/sec outer limiter — giving a sustained, measurable, linear memory growth.

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L25-28)
```rust
const CHECK_INTERVAL: Duration = Duration::from_secs(5 * 60);
const CHECK_TOKEN: u64 = 0;
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L44-44)
```rust
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
```

**File:** network/src/protocols/hole_punching/mod.rs (L66-70)
```rust
    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        self.rate_limiter.retain_recent();
        self.forward_rate_limiter.retain_recent();
        debug!("HolePunching.disconnected session={}", context.session.id);
    }
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** util/app-config/src/configs/network.rs (L355-357)
```rust
    pub fn max_inbound_peers(&self) -> u32 {
        self.max_peers.saturating_sub(self.max_outbound_peers)
    }
```

**File:** resource/ckb.toml (L94-95)
```text
max_peers = 125
max_outbound_peers = 8
```

**File:** resource/ckb.toml (L112-112)
```text
support_protocols = ["Ping", "Discovery", "Identify", "Feeler", "DisconnectMessage", "Sync", "Relay", "Time", "Alert", "LightClient", "Filter", "HolePunching"]
```
