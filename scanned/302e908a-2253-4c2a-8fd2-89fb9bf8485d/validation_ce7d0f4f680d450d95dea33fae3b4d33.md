### Title
Unauthenticated `from` Field in `ConnectionRequest` Enables Attacker-Controlled NAT Traversal via Spoofed Peer Identity — (`network/src/protocols/hole_punching/component/connection_request.rs`)

---

### Summary

The hole-punching protocol's `ConnectionRequestProcess::respond_delivered` and `ConnectionSyncProcess::execute` never verify that the `from` field in a received `ConnectionRequest` or `ConnectionSync` message matches the actual peer sending the message. An unprivileged attacker with a single legitimate P2P connection to the victim can spoof `from=target_peer_id` and `listen_addrs=[attacker_ip]`, causing the victim to insert the attacker's IP into `pending_delivered[target_peer_id]`. A subsequent spoofed `ConnectionSync` then causes the victim to spawn a NAT traversal task that calls `raw_session` to the attacker's IP, establishing a new P2P session to an attacker-controlled endpoint.

---

### Finding Description

**Step 1 — Spoofed `ConnectionRequest`:**

In `ConnectionRequestProcess::execute`, when `self_peer_id == content.to` (i.e., the victim is the intended destination), the code calls `respond_delivered(content.from, ...)` directly: [1](#0-0) 

There is no check that `content.from` matches the actual sending peer (`self.peer` / `context.session.id`). The `peer` field is only used to send the `ConnectionRequestDelivered` reply back to the attacker's session: [2](#0-1) 

After the reply is sent, the attacker's IP is unconditionally inserted into `pending_delivered` keyed by the spoofed `target_peer_id`: [3](#0-2) 

The only guard here is the `HOLE_PUNCHING_INTERVAL` (2 minutes) cooldown per `from_peer_id`, which is trivially bypassed by using a different spoofed `target_peer_id` each time: [4](#0-3) 

**Step 2 — Spoofed `ConnectionSync`:**

In `ConnectionSyncProcess::execute`, when `self_peer_id == content.to` and `route` is empty, the code looks up `pending_delivered` using `content.from` (the spoofed `target_peer_id`) as the key: [5](#0-4) 

This returns the attacker's IP that was inserted in Step 1. The code then spawns a NAT traversal task and calls `raw_session` with the attacker's IP: [6](#0-5) 

**Rate limiter analysis:**

The `forward_rate_limiter` is keyed by `(from, to, msg_item_id)`. Since `ConnectionRequest` and `ConnectionSync` have different `item_id` values, they consume separate rate limiter slots. Both can be sent within the same second without triggering the rate limit: [7](#0-6) 

---

### Impact Explanation

The victim node establishes a raw TCP session to an attacker-controlled IP. By repeating this attack with many different spoofed `target_peer_id` values (each bypasses the 2-minute cooldown), the attacker can fill the victim's outbound connection slots with attacker-controlled nodes. This constitutes a targeted eclipse attack: the victim's view of the network is dominated by attacker-controlled peers, enabling selective withholding or manipulation of block/transaction relay and causing consensus deviation.

---

### Likelihood Explanation

The attack requires only a single legitimate P2P connection to the victim — no special privileges, no key material, no majority hashpower. The spoofed fields (`from`, `listen_addrs`) are accepted verbatim from the wire message with no cryptographic binding to the sending session. The two-message sequence (one `ConnectionRequest`, one `ConnectionSync`) is sufficient to trigger `raw_session` to an arbitrary attacker-controlled IP.

---

### Recommendation

In `ConnectionRequestProcess::execute` (and `respond_delivered`), verify that `content.from` matches the actual peer ID of the sending session before inserting into `pending_delivered`. The sending peer's ID is available via the peer registry using `self.peer` (the session ID). Specifically:

```rust
// Before calling respond_delivered, verify:
let actual_from = self.protocol.network_state
    .peer_registry.read()
    .get_peer(self.peer)
    .and_then(|p| p.connected_addr.peer_id());
if actual_from.as_ref() != Some(&content.from) {
    return StatusCode::InvalidFromPeerId.with_context("from does not match sender");
}
```

Similarly, `ConnectionSyncProcess` should verify that `content.from` is the actual sending peer before consuming `pending_delivered`.

---

### Proof of Concept

```
1. Attacker connects to victim (legitimate session, session_id=S, attacker_peer_id=A)
2. Attacker sends ConnectionRequest {
       from = target_peer_id (arbitrary, not A),
       to   = victim_peer_id,
       listen_addrs = [attacker_ip:port],
       max_hops = 1,
       route = []
   }
   → victim inserts pending_delivered[target_peer_id] = ([attacker_ip:port], now)
   → victim sends ConnectionRequestDelivered back to session S (attacker receives it)

3. Attacker sends ConnectionSync {
       from  = target_peer_id,
       to    = victim_peer_id,
       route = []
   }
   → victim looks up pending_delivered[target_peer_id] = [attacker_ip:port]
   → victim spawns try_nat_traversal(bind_addr, attacker_ip:port)
   → attacker's listener accepts the TCP connection
   → victim calls raw_session(stream, attacker_ip, RawSessionInfo::inbound(...))
   → new P2P session established to attacker-controlled node

4. Repeat step 2–3 with fresh target_peer_id values to fill victim's connection table.
```

### Citations

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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L226-237)
```rust
        if let Err(error) = self
            .p2p_control
            .send_message_to(self.peer, proto_id, new_message)
            .await
        {
            return StatusCode::ForwardError.with_context(error);
        }

        let now = unix_time_as_millis();
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L144-160)
```rust
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
```

**File:** network/src/protocols/hole_punching/mod.rs (L254-257)
```rust
        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```
