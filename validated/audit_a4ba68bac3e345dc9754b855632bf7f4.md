### Title
Unbounded `pending_delivered` Growth and Uncapped `runtime::spawn` via Spoofed `from` PeerIds in HolePunching — (`network/src/protocols/hole_punching/`)

### Summary

An unprivileged peer with a single legitimate P2P connection can insert an unbounded number of entries into the `pending_delivered` HashMap by sending `ConnectionRequest` messages with N distinct spoofed `from` PeerIds. Because the only deduplication guard (`HOLE_PUNCHING_INTERVAL`) is keyed on `from`, and the `forward_rate_limiter` creates a new bucket per unique `(from, to)` pair, every unique `from` bypasses both guards. A follow-up wave of `ConnectionSync` messages then triggers one `runtime::spawn` per accumulated entry, each spawning up to 24 concurrent TCP connection attempts, causing combined memory exhaustion and async-task explosion.

---

### Finding Description

**Constants (all confirmed in source):**

| Constant | Value |
|---|---|
| `TIMEOUT` | 5 min (300 000 ms) |
| `CHECK_INTERVAL` | 5 min |
| `HOLE_PUNCHING_INTERVAL` | 2 min |
| `ADDRS_COUNT_LIMIT` | 24 |
| Session `rate_limiter` quota | 30 req/sec per `(session_id, item_id)` |
| `forward_rate_limiter` quota | 1 req/sec per `(from, to, item_id)` |

**Guard 1 — session `rate_limiter`** is keyed by `(session_id, msg.item_id())`. [1](#0-0) 
This caps the attacker at 30 `ConnectionRequest` messages per second from one session, but does **not** limit how many distinct `from` PeerIds those 30 messages carry.

**Guard 2 — `forward_rate_limiter`** is keyed by `(from, to, msg_item_id)`. [2](#0-1) 
With N unique `from` PeerIds, each gets its own independent bucket. All N pass the limiter simultaneously.

**Guard 3 — `HOLE_PUNCHING_INTERVAL` deduplication** only fires when the same `from` already exists in `pending_delivered`. [3](#0-2) 
With N unique `from` PeerIds, this check is never triggered.

**Uncapped insertion** — after a successful send-back, the entry is inserted with no size guard: [4](#0-3) 

**Cleanup** runs only every `CHECK_INTERVAL = TIMEOUT = 5 min`, so entries accumulate for the full window before any are evicted: [5](#0-4) 

**Steady-state maximum accumulation:** 30 entries/sec × 300 sec = **9 000 entries**, each holding up to 24 `Multiaddr` values.

**Task explosion** — for each `ConnectionSync{from=from_i, to=victim, route=[]}` where `from_i` is in `pending_delivered`, the victim unconditionally calls `runtime::spawn`: [6](#0-5) 
The `forward_rate_limiter` for `ConnectionSync` has the same per-`(from, to)` keying flaw, so N unique `from` PeerIds produce N spawns per second. Each spawn calls `select_ok(tasks)` over up to 24 concurrent TCP connection attempts to attacker-controlled addresses.

**Secondary unbounded growth** — the `forward_rate_limiter`'s own `HashMapStateStore` also grows one bucket per unique `(from, to)` key with no eviction between `retain_recent()` calls on disconnect: [7](#0-6) 

---

### Impact Explanation

- **Memory:** 9 000 entries × 24 `Multiaddr` (~50 bytes each) ≈ 10 MB per attacker connection. With multiple attacker connections (each limited to 30/sec), this scales linearly.
- **Async task explosion:** 9 000 `runtime::spawn` calls, each opening up to 24 outbound TCP connections to attacker-controlled IPs, can exhaust the victim's file-descriptor limit and saturate its async runtime, causing node-wide congestion and potential crash.
- **No PoW, no stake, no privileged role required** — only a standard P2P connection with the `HolePunching` protocol negotiated.

---

### Likelihood Explanation

Any peer that can establish a TCP connection to the victim and negotiate the `HolePunching` sub-protocol can execute this attack. The `from` field in `ConnectionRequest` is never validated against the actual sender's session identity, so spoofing arbitrary PeerIds is trivial. The attack is executable from a single connection within the 5-minute accumulation window.

---

### Recommendation

1. **Cap `pending_delivered` size** — reject `respond_delivered` insertions once the map exceeds a hard limit (e.g., 256 entries).
2. **Key the deduplication guard on the sending session**, not only on `from`, so a single session cannot insert more than one entry per `HOLE_PUNCHING_INTERVAL`.
3. **Rate-limit `runtime::spawn` calls** — maintain a per-node counter of in-flight NAT traversal tasks and drop new spawns when the limit is reached.
4. **Validate `from` against the sender's session PeerId** — reject `ConnectionRequest` messages where `from` does not match the actual connected peer's identity.

---

### Proof of Concept

```
// State-transition test sketch
let mut pending_delivered: HashMap<PeerId, PendingDeliveredInfo> = HashMap::new();
for i in 0..10_000u32 {
    let fake_from = PeerId::random();          // unique each iteration
    let addrs = vec!["/ip4/1.2.3.4/tcp/8115".parse::<Multiaddr>().unwrap()];
    pending_delivered.insert(fake_from, (addrs, unix_time_as_millis()));
}
// No size cap exists; assert fails:
assert!(pending_delivered.len() <= 256, "pending_delivered is unbounded: {}", pending_delivered.len());
// → panics at 10 000

// For each entry, ConnectionSync triggers runtime::spawn → 10 000 uncapped async tasks
```

The call sequence maps directly to production code:
1. Attacker → victim: `ConnectionRequest{from=from_i, to=victim, listen_addrs=[1.2.3.4:8115]}` × N (30/sec, unique `from` each time)
2. Victim: `respond_delivered` → `pending_delivered.insert(from_i, ...)` × N [4](#0-3) 
3. Attacker → victim: `ConnectionSync{from=from_i, to=victim, route=[]}` × N
4. Victim: `runtime::spawn(select_ok(nat_traversal_tasks))` × N [8](#0-7)

### Citations

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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L119-162)
```rust
                            let tasks = listens
                                .into_iter()
                                .map(|listen_addr| {
                                    Box::pin(try_nat_traversal(self.bind_addr, listen_addr))
                                })
                                .collect::<Vec<_>>();

                            if tasks.is_empty() {
                                return StatusCode::Ignore.with_context("no valid listen address");
                            }

                            debug!(
                                "current peer is the target peer {}, start NAT traversal",
                                content.to
                            );

                            match self
                                .protocol
                                .network_state
                                .config
                                .listen_addresses
                                .first()
                                .cloned()
                            {
                                Some(listen_addr) => {
                                    let control: ServiceAsyncControl = self.p2p_control.clone();
                                    runtime::spawn(async move {
                                        if let Ok(((stream, addr), _)) = select_ok(tasks).await {
                                            debug!("NAT traversal success, addr: {:?}", addr);
                                            if let Some(metrics) = ckb_metrics::handle() {
                                                metrics
                                                    .ckb_hole_punching_passive_success_count
                                                    .inc();
                                            }

                                            let _ignore = control
                                                .raw_session(
                                                    stream,
                                                    addr,
                                                    RawSessionInfo::inbound(listen_addr),
                                                )
                                                .await;
                                        }
                                    });
```
