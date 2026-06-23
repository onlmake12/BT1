### Title
Unauthenticated ConnectionSync Triggers Victim-Initiated TCP Connections to Attacker-Controlled Addresses — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

---

### Summary

An unprivileged peer connected to victim node A can, via two sequential P2P messages (`ConnectionRequest` then `ConnectionSync`), cause A to spawn up to 24 concurrent TCP connection tasks — each retrying for 30 seconds — to arbitrary attacker-controlled endpoints. Neither message validates that the `from` field matches the actual session peer ID, and no guard prevents the passive NAT traversal path from being triggered by the same peer who seeded `pending_delivered`.

---

### Finding Description

**Step 1 — Seeding `pending_delivered` via `ConnectionRequest`**

When victim A receives a `ConnectionRequest` whose `to` field equals A's own peer ID, `ConnectionRequestProcess::execute` calls `respond_delivered`: [1](#0-0) 

Inside `respond_delivered`, after filtering for TCP/IPv4/IPv6 addresses, the attacker-supplied `listen_addrs` are stored verbatim: [2](#0-1) 

The `from` field is taken directly from message content — there is no check that it matches the actual session peer ID: [3](#0-2) 

The only guard here is a 2-minute cooldown per `from_peer_id` key, which only prevents re-seeding, not the initial seed: [4](#0-3) 

**Step 2 — Triggering `try_nat_traversal` via `ConnectionSync`**

When victim A receives a `ConnectionSync` with `to = A_peer_id`, the passive path executes. It looks up `pending_delivered[content.from]` — the attacker-controlled addresses — and spawns a task running all of them concurrently: [5](#0-4) 

Again, `content.from` is taken from the message with no verification against the actual session peer ID. `ConnectionSyncProcess` has no `peer` field at all: [6](#0-5) 

**Step 3 — Each spawned task retries for 30 seconds**

Each `try_nat_traversal` call loops for a 30-second window, issuing a new TCP `connect()` every ~200ms to the target address: [7](#0-6) 

**Rate-limiter analysis**

The `forward_rate_limiter` is keyed by `(from, to, item_id)` at 1 req/sec: [8](#0-7) 

This means the attacker can send 1 `ConnectionSync` per second. Each triggers 24 concurrent 30-second tasks. After 30 seconds the victim is sustaining 30 × 24 = **720 concurrent TCP connection tasks** to attacker-chosen endpoints.

The `ADDRS_COUNT_LIMIT` is 24: [9](#0-8) 

---

### Impact Explanation

- **TCP port scanner / amplifier**: The victim makes repeated TCP `connect()` calls to arbitrary third-party hosts and ports chosen by the attacker. Each address receives ~150 connection attempts over 30 seconds.
- **Eclipse / inbound hijack**: If the attacker controls one of the target endpoints, it accepts the connection; the victim then calls `control.raw_session(stream, addr, RawSessionInfo::inbound(...))`, establishing a session the attacker controls as if it were an inbound peer. [10](#0-9) 

---

### Likelihood Explanation

The attacker needs only a single established P2P connection to victim A (standard peer connection). No special privileges, no PoW, no key material. The two messages are small and well-formed. The attack is repeatable every 2 minutes (re-seed) and the `ConnectionSync` trigger fires at 1/sec indefinitely while the seed is live (up to the 5-minute `TIMEOUT`). [11](#0-10) 

---

### Recommendation

1. **Bind `pending_delivered` to the actual session peer ID**: In `respond_delivered`, key the map on the verified session peer ID (from `context.session.id` resolved to a `PeerId`), not the attacker-supplied `content.from`.
2. **Verify sender identity in `ConnectionSync`**: Add a `peer: PeerIndex` field to `ConnectionSyncProcess` and confirm that the resolved peer ID of the session matches `content.from` before looking up `pending_delivered`.
3. **Rate-limit the passive NAT traversal trigger**: Add a per-`from` cooldown on the `ConnectionSync` passive path analogous to the `HOLE_PUNCHING_INTERVAL` guard already present in `respond_delivered`.

---

### Proof of Concept

```
1. Attacker establishes a normal P2P connection to victim A.
2. Attacker sends ConnectionRequest{
       from = <attacker_peer_id>,
       to   = <A_peer_id>,
       listen_addrs = [ip1:p1, ip2:p2, ..., ip24:p24],  // 24 attacker-controlled endpoints
       max_hops = 1
   }
   → Victim A: self_peer_id == content.to → respond_delivered() called
   → pending_delivered[attacker_peer_id] = ([ip1..ip24], now)

3. Attacker sends ConnectionSync{
       from = <attacker_peer_id>,
       to   = <A_peer_id>,
       route = []
   }
   → Victim A: self_peer_id == content.to → passive path
   → listens_info = pending_delivered[attacker_peer_id]  // 24 IPs
   → runtime::spawn(select_ok([try_nat_traversal(ip1), ..., try_nat_traversal(ip24)]))
   → 24 concurrent TCP connection tasks, each retrying for 30s

4. Repeat step 3 once per second (forward_rate_limiter allows 1/sec).
   After 30s: 720 concurrent TCP tasks to attacker-chosen hosts.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L36-38)
```rust
        let from = PeerId::from_bytes(value.from().raw_data().to_vec()).map_err(|_| {
            StatusCode::InvalidFromPeerId.with_context("the from peer id is invalid")
        })?;
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L161-166)
```rust
        if let Some((_, t)) = self.protocol.pending_delivered.get(&from_peer_id) {
            let now = unix_time_as_millis();
            if now - t < HOLE_PUNCHING_INTERVAL {
                return StatusCode::Ignore
                    .with_context("a same message is already replied in a moment ago");
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L154-160)
```rust
                                            let _ignore = control
                                                .raw_session(
                                                    stream,
                                                    addr,
                                                    RawSessionInfo::inbound(listen_addr),
                                                )
                                                .await;
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
