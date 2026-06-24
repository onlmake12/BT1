Looking at the actual code to validate each claim.

Audit Report

## Title
`pending_delivered` Entry Not Consumed After `ConnectionSync` Processing Allows Unbounded NAT Traversal Task Spawning - (File: `network/src/protocols/hole_punching/component/connection_sync.rs`)

## Summary
In `ConnectionSyncProcess::execute`, when the target node processes a `ConnectionSync` message and spawns a NAT traversal task, the `pending_delivered` map entry for `content.from` is read via `.get()` but never removed or cleared. Combined with a `forward_rate_limiter` that permits 1 request/second per `(from, to)` pair and a 5-minute `TIMEOUT` window, an attacker can spawn up to 300 concurrent `try_nat_traversal` tasks — each making ~150 TCP connection attempts over 30 seconds — exhausting file descriptors and outbound connection capacity on the victim node.

## Finding Description

`HolePunching` maintains `pending_delivered: HashMap<PeerId, PendingDeliveredInfo>` where `PendingDeliveredInfo = (Vec<Multiaddr>, u64)`. [1](#0-0) 

In `connection_request.rs`, `respond_delivered` inserts the entry with the attacker's listen addresses and a timestamp when the target processes a `ConnectionRequest` addressed to itself: [2](#0-1) 

In `connection_sync.rs`, when the target node is the `to` peer, it reads the entry with `.get()` and clones the listen addresses to spawn NAT traversal tasks — **the entry is never removed or cleared after use**: [3](#0-2) 

The only protection against repeated triggering is the `forward_rate_limiter`, keyed by `(content.from, content.to, msg_item_id)` at 1 request/second: [4](#0-3) [5](#0-4) 

The `pending_delivered` entry only expires via the periodic `notify()` cleanup after `TIMEOUT` = 5 minutes from the **insertion** timestamp — not from each `ConnectionSync` invocation: [6](#0-5) 

Each spawned `try_nat_traversal` task runs for up to 30 seconds, making a TCP connection attempt every ~200ms (~150 attempts per task): [7](#0-6) 

## Impact Explanation

An attacker can spawn up to 300 `try_nat_traversal` tasks (1/second × 300 seconds) per `(from, to)` pair per 5-minute window. At peak, ~30 tasks run concurrently, each making ~150 TCP connection attempts, totalling ~4,500 outbound TCP attempts simultaneously. This exhausts the victim node's file descriptors, outbound connection slots, and CPU, causing a node crash or severe degradation.

**Impact class: High — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

The attack requires only a direct P2P connection to the target node and the ability to send `HolePunching` protocol messages — both available to any unprivileged peer. The `ConnectionRequest` step is trivially satisfied since the attacker sets `to = target_peer_id` and is directly connected. No special privileges, keys, or majority hashpower are needed. The attack is repeatable every 5 minutes as the `pending_delivered` entry expires and can be re-created.

## Recommendation

After `ConnectionSync` is processed and NAT traversal is spawned, clear the listen addresses from the `pending_delivered` entry to prevent repeated triggering while preserving the timestamp for the `respond_delivered` cooldown in `connection_request.rs`:

```rust
// In connection_sync.rs, after runtime::spawn(...):
if let Some(entry) = self.protocol.pending_delivered.get_mut(&content.from) {
    entry.0.clear(); // Consume the listen addresses; keep timestamp for cooldown
}
```

Alternatively, maintain a separate `HashSet<PeerId>` tracking peers for which NAT traversal has already been initiated within the current session, and skip re-triggering if the peer is already present.

## Proof of Concept

```
Attacker (peer A) → directly connected to Target (peer T)

1. A sends: ConnectionRequest { from=A, to=T, listen_addrs=[addr1, addr2, ...], max_hops=6 }
   T processes: respond_delivered(A, T, [addr1, addr2, ...])
     → pending_delivered[A] = ([addr1, addr2, ...], now)
     → sends ConnectionRequestDelivered back to A

2. A sends: ConnectionSync { from=A, to=T, route=[] }  ← 1 per second
   T processes: ConnectionSyncProcess::execute()
     → forward_rate_limiter.check_key((A, T, SYNC_ID)) → OK (1/sec)
     → pending_delivered.get(A) → Some([addr1, addr2, ...])  ← NOT removed
     → runtime::spawn(try_nat_traversal(...))                 ← new task spawned
     → returns Status::ok()

3. Repeat step 2 every second for 5 minutes:
   → 300 NAT traversal tasks spawned
   → Each task: 30s timeout, TCP connect every ~200ms → ~150 TCP attempts
   → Peak concurrent tasks: ~30 → ~4,500 TCP attempts in flight
   → File descriptor exhaustion → node crash
```

### Citations

**File:** network/src/protocols/hole_punching/mod.rs (L30-44)
```rust
type PendingDeliveredInfo = (Vec<Multiaddr>, u64);
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;

/// Hole Punching Protocol
pub(crate) struct HolePunching {
    network_state: Arc<NetworkState>,
    bind_addr: Option<SocketAddr>,
    // Request timestamp recorded
    inflight_requests: HashMap<PeerId, u64>,
    // Delivered timestamp recorded
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
```

**File:** network/src/protocols/hole_punching/mod.rs (L172-175)
```rust
        let now = unix_time_as_millis();
        self.pending_delivered
            .retain(|_, (_, t)| (now - *t) < TIMEOUT);
        self.inflight_requests.retain(|_, t| (now - *t) < TIMEOUT);
```

**File:** network/src/protocols/hole_punching/mod.rs (L255-257)
```rust
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L85-96)
```rust
        if self
            .protocol
            .forward_rate_limiter
            .check_key(&(content.from.clone(), content.to.clone(), self.msg_item_id))
            .is_err()
        {
            debug!(
                "from: {}, to {}, item_name: {}, rate limit is reached",
                content.from, content.to, "ConnectionSync",
            );
            return StatusCode::TooManyRequests.with_context("ConnectionSync");
        }
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-115)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L62-68)
```rust
    let base_retry_interval = Duration::from_millis(200);

    // total time
    let timeout_duration = Duration::from_secs(30);
    let start_time = Instant::now();
    let mut retry_count = 0u32;
    while start_time.elapsed() < timeout_duration {
```
