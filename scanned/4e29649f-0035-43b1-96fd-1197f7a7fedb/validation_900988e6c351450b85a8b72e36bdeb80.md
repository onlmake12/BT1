Audit Report

## Title
Unauthenticated `ConnectionRequest`/`ConnectionSync` Allows Arbitrary TCP Connection Storms via Unverified `from` Field — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

## Summary

An attacker with a single established P2P connection to victim node A can cause A to spawn unbounded concurrent TCP connection tasks to attacker-controlled endpoints. The root cause is that neither `ConnectionRequestProcess` nor `ConnectionSyncProcess` verifies that `content.from` matches the actual session peer ID. This allows the attacker to seed `pending_delivered` with arbitrary addresses via `ConnectionRequest`, then repeatedly trigger `try_nat_traversal` via `ConnectionSync`, accumulating hundreds of concurrent 30-second TCP retry loops.

## Finding Description

**Root cause — unverified `content.from` in `ConnectionRequest`:**

`ConnectionRequestProcess` holds a `peer: PeerIndex` field representing the actual session, but `respond_delivered` is called with `content.from` taken directly from the message body without any check that it matches the session's resolved peer ID. [1](#0-0) [2](#0-1) 

The only guard in `respond_delivered` is a 2-minute cooldown keyed on `from_peer_id` — it prevents re-seeding the same key, not the initial seed: [3](#0-2) 

After filtering for TCP/IPv4/IPv6, the attacker-supplied addresses are stored verbatim: [4](#0-3) 

**Root cause — no peer identity in `ConnectionSyncProcess`:**

`ConnectionSyncProcess` has no `peer` field at all, so there is no mechanism to verify `content.from` against the actual session: [5](#0-4) 

When `route` is empty and `self_peer_id == content.to`, the passive path looks up `pending_delivered[content.from]` — the attacker-controlled addresses — and spawns a task running all of them concurrently via `select_ok`: [6](#0-5) 

**`try_nat_traversal` retries for 30 seconds per address:**

Each future loops for a 30-second window, issuing a new TCP `connect()` with a 200ms timeout every ~200ms: [7](#0-6) 

**Rate limiter analysis:**

The `forward_rate_limiter` is keyed by `(from, to, item_id)` at 1 req/sec: [8](#0-7) 

The per-session `rate_limiter` allows 30 req/sec per `(session_id, msg_type)`. Since the attacker controls `content.from`, they can use distinct `from` values to bypass the `forward_rate_limiter` entirely, sending up to 30 `ConnectionSync` messages per second (each with a different `from` that was pre-seeded). Each triggers a `runtime::spawn` of 24 concurrent `try_nat_traversal` futures. After 30 seconds: **30 spawned tasks/sec × 30 sec × 24 futures = 21,600 concurrent TCP retry loops** in the worst case. Even at the conservative 1/sec rate claimed: 30 × 24 = 720 concurrent tasks.

`ADDRS_COUNT_LIMIT` caps addresses per seed at 24: [9](#0-8) 

`TIMEOUT` keeps seeds alive for 5 minutes: [10](#0-9) 

**Existing checks that fail:**

- The `route.contains(self_peer_id)` check in `ConnectionRequest` only prevents routing loops, not identity spoofing.
- The 2-minute `HOLE_PUNCHING_INTERVAL` cooldown in `respond_delivered` only prevents re-seeding the same `from_peer_id` key; the attacker can use a fresh `from_peer_id` each cycle.
- The `forward_rate_limiter` is trivially bypassed by varying `content.from`.

## Impact Explanation

The victim node exhausts file descriptors and async task resources through attacker-directed TCP connection storms to arbitrary third-party hosts. Each `try_nat_traversal` holds an open socket for up to 200ms per iteration across a 30-second window. At 720–21,600 concurrent futures, this can exhaust the OS file descriptor limit (typically 1024 soft / 65536 hard on Linux), causing the node process to fail to open new connections or crash. This matches: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node**, and secondarily **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, since the victim's outbound TCP SYNs flood third-party hosts chosen by the attacker.

## Likelihood Explanation

The attacker requires only a single standard P2P connection — no special privileges, no PoW, no key material. The two message types (`ConnectionRequest`, `ConnectionSync`) are small and well-formed. The seed phase requires one `ConnectionRequest` per unique `from_peer_id` (subject to a 2-minute cooldown per key, but the attacker can use unlimited distinct keys). The trigger phase fires at up to 30/sec (session rate limiter) indefinitely while seeds remain live (5-minute `TIMEOUT`). The attack is fully repeatable and requires no victim interaction beyond accepting the initial P2P connection.

## Recommendation

1. **Bind `pending_delivered` to the verified session peer ID**: In `respond_delivered`, resolve the actual peer ID from `context.session.id` via the peer registry and use that as the map key, discarding `content.from` for this purpose.
2. **Add a `peer` field to `ConnectionSyncProcess`**: Pass the session's resolved `PeerId` and assert it equals `content.from` before looking up `pending_delivered`.
3. **Rate-limit the passive NAT traversal trigger per session**: Add a per-session (not per-`from`) cooldown on the `ConnectionSync` passive path, analogous to `HOLE_PUNCHING_INTERVAL` in `respond_delivered`.
4. **Cap concurrent spawned traversal tasks**: Track the number of in-flight `runtime::spawn` NAT traversal tasks and reject new triggers when a per-node limit is reached.

## Proof of Concept

```
1. Attacker establishes a normal P2P connection to victim A.

2. For i in 1..N (N distinct attacker_peer_ids):
   Attacker sends ConnectionRequest{
       from = attacker_peer_id_i,       // arbitrary, not verified against session
       to   = victim_peer_id,
       listen_addrs = [ip1:p1, ..., ip24:p24],  // 24 attacker-controlled endpoints
       max_hops = 1
   }
   → victim: self_peer_id == content.to → respond_delivered()
   → pending_delivered[attacker_peer_id_i] = ([ip1..ip24], now)
   (No cooldown since each key is fresh; rate_limiter allows 30 seeds/sec)

3. Attacker sends 30 ConnectionSync/sec (session rate limit), each with a
   distinct from = attacker_peer_id_i (bypassing forward_rate_limiter):
   ConnectionSync{ from=attacker_peer_id_i, to=victim_peer_id, route=[] }
   → victim: passive path → pending_delivered[attacker_peer_id_i] found
   → runtime::spawn(select_ok([try_nat_traversal(ip1), ..., try_nat_traversal(ip24)]))

4. After 30 seconds:
   30 spawns/sec × 30 sec × 24 futures = 21,600 concurrent TCP retry loops
   Each loop holds a socket open for up to 200ms per iteration.
   File descriptor exhaustion → node crash or inability to accept/open connections.

Verification: Monitor victim's open file descriptors (lsof -p <pid> | wc -l)
and async task count; observe exponential growth until crash or FD limit hit.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L85-91)
```rust
pub(crate) struct ConnectionRequestProcess<'a> {
    message: packed::ConnectionRequestReader<'a>,
    protocol: &'a mut HolePunching,
    peer: PeerIndex,
    p2p_control: &'a ServiceAsyncControl,
    msg_item_id: u32,
}
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L51-57)
```rust
pub(crate) struct ConnectionSyncProcess<'a> {
    message: packed::ConnectionSyncReader<'a>,
    protocol: &'a HolePunching,
    p2p_control: &'a ServiceAsyncControl,
    bind_addr: Option<SocketAddr>,
    msg_item_id: u32,
}
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-124)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());

                    match listens_info {
                        Some(listens) => {
                            let tasks = listens
                                .into_iter()
                                .map(|listen_addr| {
                                    Box::pin(try_nat_traversal(self.bind_addr, listen_addr))
                                })
                                .collect::<Vec<_>>();
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L65-68)
```rust
    let timeout_duration = Duration::from_secs(30);
    let start_time = Instant::now();
    let mut retry_count = 0u32;
    while start_time.elapsed() < timeout_duration {
```

**File:** network/src/protocols/hole_punching/mod.rs (L27-27)
```rust
const ADDRS_COUNT_LIMIT: usize = 24;
```

**File:** network/src/protocols/hole_punching/mod.rs (L28-28)
```rust
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L255-257)
```rust
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```
