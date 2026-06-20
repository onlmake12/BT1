Looking at the full execution path across all relevant files.

### Title
Missing `from != to` Validation in `ConnectionRequestProcess::execute` Allows Forced NAT Traversal to Attacker-Controlled Addresses — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

`ConnectionRequestProcess::execute` has no check that `from != to`. An attacker who is a connected peer can send a `ConnectionRequest` with both `from` and `to` set to the local node's own `PeerId`. The existing loop-detection guard only checks whether `self_peer_id` appears in the `route` field — a separate list — so it does not catch this case. The node then calls `respond_delivered` with `from_peer_id == to_peer_id == self_peer_id`, inserts a self-referential entry into `pending_delivered`, and a follow-up `ConnectionSync` from the same attacker triggers `try_nat_traversal` against attacker-controlled addresses for up to 30 seconds of repeated TCP connection attempts.

---

### Finding Description

**Step 1 — `ConnectionRequest` with `from == to == self_peer_id`**

In `execute()`, the only loop-detection guard is:

```rust
if content.route.contains(self_peer_id) {
    return StatusCode::Ignore.with_context("the message is passed, ignore it");
}
``` [1](#0-0) 

`from` and `to` are separate fields from `route`; setting `from = to = self_peer_id` with an empty `route` passes this check entirely.

**Step 2 — Branch taken: `self_peer_id == &content.to`**

```rust
if self_peer_id == &content.to {
    self.respond_delivered(content.from, &content.to, content.listen_addrs).await
``` [2](#0-1) 

Because `to == self_peer_id`, the node believes it is the intended destination and calls `respond_delivered` with `from_peer_id = self_peer_id`.

**Step 3 — Self-referential insertion into `pending_delivered`**

```rust
self.protocol
    .pending_delivered
    .insert(from_peer_id, (remote_listens, now));
``` [3](#0-2) 

`pending_delivered` is now `{ self_peer_id → ([attacker_addresses], now) }`. The attacker controls `remote_listens` via the `listen_addrs` field of the original message (filtered to TCP/IP only, but otherwise unconstrained).

**Step 4 — `ConnectionSync` triggers NAT traversal to attacker addresses**

The attacker sends a `ConnectionSync` with `from = to = self_peer_id` and an empty `route`. In `ConnectionSyncProcess::execute`:

```rust
let listens_info = self
    .protocol
    .pending_delivered
    .get(&content.from)          // content.from == self_peer_id
    .map(|info| info.0.clone());
``` [4](#0-3) 

This retrieves the attacker-inserted entry. The node then spawns:

```rust
runtime::spawn(async move {
    if let Ok(((stream, addr), _)) = select_ok(tasks).await {
        let _ignore = control
            .raw_session(stream, addr, RawSessionInfo::inbound(listen_addr))
            .await;
    }
});
``` [5](#0-4) 

**Step 5 — `try_nat_traversal` retries for 30 seconds**

```rust
let timeout_duration = Duration::from_secs(30);
// ... retry loop with 200ms intervals
``` [6](#0-5) 

Each `ConnectionSync` the attacker sends spawns a 30-second background task making repeated TCP connection attempts to attacker-controlled addresses.

---

### Impact Explanation

1. **State corruption**: `pending_delivered` holds a self-referential entry keyed by `self_peer_id`, violating the invariant that `from` and `to` must be distinct peers.
2. **Forced outbound TCP connections**: The node makes repeated outbound TCP connections to attacker-specified addresses for up to 30 seconds per `ConnectionSync` message.
3. **Raw session establishment as inbound**: If the TCP connection succeeds, `raw_session(..., RawSessionInfo::inbound(...))` is called, establishing a session that the node treats as inbound — potentially bypassing outbound connection limits and peer selection logic.
4. **Resource exhaustion**: Each `ConnectionSync` spawns a background task; the per-session rate limiter allows up to 30 messages/second, meaning up to 30 concurrent 30-second retry tasks per attacker session.

---

### Likelihood Explanation

- **Precondition**: Any connected peer. No special role (relay, miner, etc.) is required.
- **Peer ID is public**: `PeerId` values are exchanged during normal peer discovery, so the attacker trivially knows the target's `self_peer_id`.
- **Two messages suffice**: One `ConnectionRequest` + one `ConnectionSync` triggers the full exploit path.
- **Rate limiting is insufficient**: The `forward_rate_limiter` key is `(from, to, item_id)`. With `from == to == self_peer_id`, varying `item_id` allows repeated insertions; the per-session limiter caps at 30/s but does not prevent the attack.

---

### Recommendation

Add an explicit `from != to` guard immediately after parsing `RequestContent`, before any routing logic:

```rust
if content.from == content.to {
    return StatusCode::InvalidFromPeerId
        .with_context("from and to must be distinct peers");
}
```

This should be placed at the top of `execute()` in `connection_request.rs`, and analogously in `connection_request_delivered.rs` and `connection_sync.rs` for defense in depth.

---

### Proof of Concept

```
1. Attacker connects to victim node as a normal peer.
2. Attacker sends ConnectionRequest {
       from = victim_peer_id,
       to   = victim_peer_id,
       listen_addrs = [attacker_ip:attacker_port],
       route = [],
       max_hops = 6
   }
3. Victim: route.contains(self_peer_id) → false (route is empty) → passes.
4. Victim: self_peer_id == content.to → true → calls respond_delivered.
5. Victim: pending_delivered.insert(victim_peer_id, ([attacker_ip:attacker_port], now)).
6. Attacker sends ConnectionSync {
       from  = victim_peer_id,
       to    = victim_peer_id,
       route = []
   }
7. Victim: self_peer_id == content.to → true → looks up pending_delivered[victim_peer_id].
8. Victim: spawns try_nat_traversal task → makes repeated TCP connections to attacker_ip:attacker_port for 30 seconds.
9. Assert: pending_delivered contains self-referential entry; attacker observes inbound TCP connections.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L127-130)
```rust
        let self_peer_id = self.protocol.network_state.local_peer_id();
        if content.route.contains(self_peer_id) {
            return StatusCode::Ignore.with_context("the message is passed, ignore it");
        }
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
```

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L235-237)
```rust
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-115)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L145-162)
```rust
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

**File:** network/src/protocols/hole_punching/component/mod.rs (L65-68)
```rust
    let timeout_duration = Duration::from_secs(30);
    let start_time = Instant::now();
    let mut retry_count = 0u32;
    while start_time.elapsed() < timeout_duration {
```
