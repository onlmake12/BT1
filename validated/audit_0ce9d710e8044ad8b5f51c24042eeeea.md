### Title
Missing `from != to` Guard in `ConnectionSyncProcess::execute` Enables Attacker-Triggered Self-Referential NAT Traversal Task Accumulation — (`network/src/protocols/hole_punching/component/connection_sync.rs`)

---

### Summary

`ConnectionSyncProcess::execute` never validates that `content.from != content.to`. A single connected peer can first seed `pending_delivered[victim_peer_id]` via a spoofed `ConnectionRequest{from=victim, to=victim}`, then repeatedly send `ConnectionSync{from=victim, to=victim, route=[]}` at the rate-limiter ceiling (1 msg/sec), causing the victim to spawn a new 30-second NAT traversal task every second — accumulating up to 30 concurrent tasks, each hammering attacker-controlled TCP addresses.

---

### Finding Description

**Step 1 — Seed `pending_delivered[victim_peer_id]`**

In `ConnectionRequestProcess::execute`: [1](#0-0) 

When `content.to == self_peer_id`, `respond_delivered(content.from, ...)` is called. There is no `from != to` guard. If the attacker sends `ConnectionRequest{from=victim_peer_id, to=victim_peer_id, listen_addrs=[attacker_ip:port]}`, the victim satisfies `self_peer_id == content.to` and inserts: [2](#0-1) 

This populates `pending_delivered[victim_peer_id]` with attacker-controlled TCP addresses.

**Step 2 — Trigger NAT traversal tasks**

In `ConnectionSyncProcess::execute`, when `route` is empty and `self_peer_id == content.to`: [3](#0-2) 

The lookup key is `content.from`, which equals `victim_peer_id` — hitting the entry planted in Step 1. The code then spawns a `runtime::spawn` task: [4](#0-3) 

**No `from != to` check exists anywhere in this path.**

**Rate limiter and task accumulation**

The `forward_rate_limiter` key is `(content.from, content.to, msg_item_id)`: [5](#0-4) 

This allows 1 `ConnectionSync{from=victim, to=victim}` per second. Each spawned NAT traversal task runs for up to **30 seconds**, retrying every ~200 ms: [6](#0-5) 

At 1 task/sec × 30-second lifetime = **up to 30 concurrent tasks** accumulate, each making ~150 TCP connection attempts to attacker-controlled addresses.

---

### Impact Explanation

- **Socket exhaustion**: 30 concurrent tasks × ~150 TCP `connect()` calls each = thousands of in-flight TCP attempts against attacker-controlled endpoints.
- **Metrics pollution**: `ckb_hole_punching_passive_count` is incremented on every triggered execution.
- **Sustained with one connection**: The attacker needs only a single P2P session to the victim; no PoW, no stake, no privilege.
- **Attacker-directed connections**: Because `pending_delivered` stores attacker-supplied `listen_addrs`, the victim's TCP stack is directed at arbitrary IP:port pairs, enabling amplification or port-scanning side effects.

---

### Likelihood Explanation

The precondition (populating `pending_delivered[victim_peer_id]`) is trivially satisfied by the same attacker in the same session via a prior `ConnectionRequest` with spoofed `from=victim_peer_id`. Both message types accept arbitrary `from`/`to` peer IDs with no authentication against the actual session's peer identity. Any peer that can open a HolePunching protocol session can execute this.

---

### Recommendation

Add an explicit `from != to` guard at the top of both `ConnectionSyncProcess::execute` and `ConnectionRequestProcess::execute`:

```rust
if content.from == content.to {
    return StatusCode::Ignore.with_context("from and to must differ");
}
```

Additionally, validate that `content.from` or `content.to` matches the actual session's authenticated peer ID where applicable, to prevent peer ID spoofing in these fields entirely.

---

### Proof of Concept

```
1. Attacker opens a HolePunching protocol session to victim node V.

2. Attacker sends:
   ConnectionRequest {
       from = V.peer_id,
       to   = V.peer_id,
       listen_addrs = [attacker_ip:attacker_port],  // valid TCP addr
       route = [],
       max_hops = 1,
   }
   → V sees self_peer_id == content.to, calls respond_delivered(V.peer_id, ...),
     inserts pending_delivered[V.peer_id] = ([attacker_ip:attacker_port], now).

3. Attacker sends (once per second, indefinitely):
   ConnectionSync {
       from  = V.peer_id,
       to    = V.peer_id,
       route = [],
   }
   → V sees self_peer_id == content.to, looks up pending_delivered[V.peer_id],
     spawns runtime::spawn(try_nat_traversal(bind_addr, attacker_ip:attacker_port)).

4. After 30 seconds, 30 concurrent 30-second NAT traversal tasks are running,
   each making TCP connect() attempts to attacker_ip:attacker_port every ~200ms.
   Attacker observes inbound TCP SYNs confirming the exploit.
```

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L145-147)
```rust
        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L144-163)
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
                                        }
                                    });
                                    Status::ok()
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L65-68)
```rust
    let timeout_duration = Duration::from_secs(30);
    let start_time = Instant::now();
    let mut retry_count = 0u32;
    while start_time.elapsed() < timeout_duration {
```
