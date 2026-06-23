### Title
Unbounded `runtime::spawn` NAT Traversal Tasks via Spoofed `ConnectionRequest`/`ConnectionSync` — (`network/src/protocols/hole_punching/component/connection_sync.rs`, `connection_request.rs`)

---

### Summary

An unprivileged remote peer can exhaust the victim node's file descriptors and TCP connection slots by (1) poisoning `pending_delivered` with up to 24 attacker-controlled TCP addresses per entry using spoofed `ConnectionRequest` messages, then (2) triggering unbounded `runtime::spawn` tasks via `ConnectionSync` messages. Each spawned task runs 24 concurrent `try_nat_traversal` futures that each retry TCP connections for 30 seconds. No guard bounds the total number of spawned tasks or the total number of concurrent TCP sockets opened.

---

### Finding Description

**Step 1 — Poisoning `pending_delivered`**

`ConnectionRequestProcess::execute` in `connection_request.rs` processes any inbound `ConnectionRequest` where `content.to == local_peer_id`. It calls `respond_delivered(content.from, ...)`, which stores the attacker-supplied `listen_addrs` into `pending_delivered` keyed by `content.from`: [1](#0-0) [2](#0-1) 

There is **no check** that `content.from` matches the actual session's peer ID. The attacker can set `from` to any arbitrary peer ID. The only per-`from` guard is `HOLE_PUNCHING_INTERVAL` (2 minutes), which is trivially bypassed by using a different `from` value for each message: [3](#0-2) 

The top-level `rate_limiter` allows 30 messages per second per `(session_id, msg_item_id)`, so an attacker can insert 30 distinct entries into `pending_delivered` per second, each holding up to 24 TCP addresses (after the TCP/IP4/IP6 filter at lines 196–215). [4](#0-3) [5](#0-4) 

**Step 2 — Triggering unbounded spawns via `ConnectionSync`**

`ConnectionSyncProcess::execute` in `connection_sync.rs`, when `content.to == local_peer_id`, looks up `pending_delivered[content.from]` and, if found, unconditionally calls `runtime::spawn` with a `select_ok` over all stored addresses: [6](#0-5) [7](#0-6) [8](#0-7) 

The `forward_rate_limiter` (1/sec per `(from, to, item_id)`) is bypassed by using different `from` values matching the different poisoned entries. There is no global cap on the number of concurrent spawned tasks.

**Step 3 — Resource exhaustion inside `try_nat_traversal`**

Each `try_nat_traversal` future runs a retry loop for 30 seconds, creating a new `TcpSocket` and attempting `socket.connect()` every ~200ms: [9](#0-8) [10](#0-9) 

There is no semaphore, no global concurrency limit, and no cap on the number of open sockets across all `try_nat_traversal` instances.

---

### Impact Explanation

- **30 poisoned entries/second** × **24 addresses/entry** = **720 concurrent 30-second TCP retry loops** per second of attack.
- Each loop creates a new `TcpSocket` every ~200ms → ~150 sockets per address over 30 seconds.
- At steady state (after 30 seconds of attack): 30 × 30 = 900 active spawned tasks, each holding 24 concurrent socket-creating loops → up to **21,600 concurrent TCP connection attempts** at any moment.
- This exhausts the OS file descriptor table (default Linux limit: 1024 or 65536) and TCP ephemeral port space, causing the node to fail to accept or initiate any legitimate connections.

---

### Likelihood Explanation

The attack requires only a single P2P connection to the victim. No privileged role, no PoW, no key material. The `ConnectionRequest` and `ConnectionSync` messages are standard production P2P protocol messages. The spoofed `from` field is never authenticated against the session. The attack is locally reproducible and requires no Sybil capability — a single connected peer suffices.

---

### Recommendation

1. **Authenticate `from`**: In `ConnectionRequestProcess::execute`, verify that `content.from` matches the peer ID of the actual session (`self.peer`). Reject messages where `from` does not match the sender.
2. **Cap `pending_delivered`**: Enforce a maximum size on the `pending_delivered` HashMap (e.g., 64 entries total), evicting oldest entries when the cap is reached.
3. **Global concurrency limit on NAT traversal**: Use a `tokio::sync::Semaphore` to bound the total number of concurrent `try_nat_traversal` tasks (e.g., max 8 or 16 globally).
4. **Remove stale entries on `ConnectionSync`**: After consuming a `pending_delivered` entry in `ConnectionSyncProcess::execute`, remove it from the map to prevent re-triggering.

---

### Proof of Concept

```
1. Attacker connects to victim node (single TCP session).
2. For i in 0..30:
     Send ConnectionRequest {
       from: random_peer_id_i,   // spoofed, not the attacker's real peer ID
       to: victim_peer_id,
       listen_addrs: [24 × "1.2.3.4:PORT_i_j"],  // valid TCP/IP4 addresses
       max_hops: 6,
       route: [],
     }
   → victim stores pending_delivered[random_peer_id_i] = ([24 addrs], now)
   → rate_limiter allows 30/sec; forward_rate_limiter allows 1/sec per (from,to,id)
     but each message has a distinct from, so all 30 pass.

3. For i in 0..30:
     Send ConnectionSync {
       from: random_peer_id_i,
       to: victim_peer_id,
       route: [],
     }
   → victim finds pending_delivered[random_peer_id_i], spawns runtime::spawn
     with select_ok([24 × try_nat_traversal(...)]).

4. Each try_nat_traversal runs for 30s, creating a TcpSocket every ~200ms.
   30 spawns × 24 futures × ~150 socket attempts = ~108,000 TCP connect()
   calls over 30 seconds. Repeat every 30s to maintain pressure.

5. Assert: victim's /proc/self/fd count approaches ulimit -n; legitimate
   connections are refused with EMFILE or ECONNREFUSED.
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

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L234-237)
```rust
        let now = unix_time_as_millis();
        self.protocol
            .pending_delivered
            .insert(from_peer_id, (remote_listens, now));
```

**File:** network/src/protocols/hole_punching/mod.rs (L27-27)
```rust
const ADDRS_COUNT_LIMIT: usize = 24;
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-115)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L118-124)
```rust
                        Some(listens) => {
                            let tasks = listens
                                .into_iter()
                                .map(|listen_addr| {
                                    Box::pin(try_nat_traversal(self.bind_addr, listen_addr))
                                })
                                .collect::<Vec<_>>();
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L144-162)
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
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L62-84)
```rust
    let base_retry_interval = Duration::from_millis(200);

    // total time
    let timeout_duration = Duration::from_secs(30);
    let start_time = Instant::now();
    let mut retry_count = 0u32;
    while start_time.elapsed() < timeout_duration {
        retry_count += 1;

        // Add a small amount of random jitter (±25ms) to avoid conflicts
        // caused by continuous precise synchronization
        let jitter = Duration::from_millis(rand::random::<u64>() % 50);
        let actual_interval = if rand::random::<bool>() {
            base_retry_interval + jitter
        } else {
            base_retry_interval.saturating_sub(jitter)
        };

        let socket = create_socket(bind_addr, net_addr)?;

        match runtime::timeout(
            std::time::Duration::from_millis(200),
            socket.connect(net_addr),
```
