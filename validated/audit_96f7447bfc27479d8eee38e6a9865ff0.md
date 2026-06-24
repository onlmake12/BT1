Audit Report

## Title
Missing `from != to` Validation Enables Resource Exhaustion via Self-Addressed Hole-Punching — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary
Any connected P2P peer can send a `ConnectionRequest` with `from == to == victim_node_peer_id` and attacker-controlled TCP addresses, causing the victim to insert a `pending_delivered` entry keyed by its own peer ID. A subsequent stream of `ConnectionSync` messages (rate-limited to 1/sec) each spawn a 30-second background task running up to 24 concurrent TCP connection futures against attacker-controlled addresses. After 30 seconds, 30 concurrent tasks × 24 concurrent futures = 720 concurrent async tasks consuming file descriptors and async runtime resources, constituting a sustained node-level DoS.

## Finding Description

**Root cause:** No `from != to` guard exists anywhere in the hole-punching message handlers.

**Step 1 — Poison `pending_delivered` via `ConnectionRequest` with `from == to == self_peer_id`:**

In `ConnectionRequestProcess::execute()`:
- L115: `listen_addrs` length check passes with 1–24 attacker-supplied TCP/IP addresses.
- L128: `content.route.contains(self_peer_id)` passes with an empty route.
- L132–143: `forward_rate_limiter` passes on first request.
- L145: `self_peer_id == &content.to` evaluates **TRUE** → `respond_delivered(content.from, ...)` is called with `from_peer_id = self_peer_id`. [1](#0-0) 

Inside `respond_delivered`:
- L161–166: The `HOLE_PUNCHING_INTERVAL` guard only fires if an entry already exists; on first call it is absent.
- L196–215: Attacker-supplied addresses are filtered to TCP/IP-only — trivially satisfied.
- L217: `remote_listens.is_empty()` is FALSE → execution continues.
- L234–237: `pending_delivered.insert(self_peer_id, (attacker_tcp_addrs, now))` — the map is now poisoned. [2](#0-1) 

**Step 2 — Trigger NAT traversal via `ConnectionSync` with `from == to == self_peer_id`, empty route:**

In `ConnectionSyncProcess::execute()`:
- L82–96: Route length and `forward_rate_limiter` checks pass (1 req/sec allowed for the `(self_peer_id, self_peer_id, msg_item_id)` key).
- L98: `content.route.last()` is `None` (empty route) → enters the `None` branch.
- L102: `self_peer_id != &content.to` is **FALSE** → enters the "current node is the `to` target" branch.
- L114: `pending_delivered.get(&content.from)` where `content.from == self_peer_id` → **finds the poisoned entry**. [3](#0-2) 

- L119–124: Creates up to 24 `try_nat_traversal` futures (one per stored address).
- L145: `runtime::spawn(...)` launches a background task running `select_ok(tasks)` — all 24 futures run concurrently for up to 30 seconds. [4](#0-3) 

Critically, the `pending_delivered` entry is only `.get()`-read, never `.remove()`-d after use, so it persists for `TIMEOUT = 5 minutes`, allowing the attacker to keep triggering new tasks for the entire window. [5](#0-4) 

**`try_nat_traversal` resource cost per task:**

Each future loops for up to 30 seconds, creating a new `TcpSocket`, attempting a 200ms-timeout connect, then sleeping ~400ms before the next iteration. With 24 futures running concurrently via `select_ok`, each spawned task holds up to 24 open sockets simultaneously during connect phases. [6](#0-5) 

**Rate limiter math:**

The `forward_rate_limiter` (1 req/sec for the same `(from, to, msg_item_id)` key) allows 1 `ConnectionSync` per second. Each spawns a 30-second task. After 30 seconds: **30 concurrent tasks × 24 concurrent TCP futures = 720 concurrent async tasks / open sockets**. [7](#0-6) 

## Impact Explanation

**High — Vulnerabilities which could easily crash a CKB node (10001–15000 points).**

- **File descriptor exhaustion**: 720 concurrent TCP sockets approaches the typical non-root process fd limit (~1024). Once exhausted, the node cannot accept new P2P connections, open database files, or perform any fd-requiring operation — effective DoS.
- **Async runtime pressure**: 720 concurrent tokio tasks continuously polling TCP futures consume CPU and memory.
- **SSRF-like outbound connections**: The node makes TCP connections to arbitrary attacker-controlled IP:port combinations, enabling internal network port scanning.
- **Sustained attack window**: The `pending_delivered` entry persists for 5 minutes; the attacker needs only 1 `ConnectionRequest` followed by sustained `ConnectionSync` messages to maintain the attack.

## Likelihood Explanation

The attacker only needs to be a connected P2P peer — no special privileges required. The `HolePunching` protocol is enabled by default when `SupportProtocol::HolePunching` is in the node config. [8](#0-7) 

The two-message sequence is trivial to craft. The only precondition is supplying at least one valid TCP/IP multiaddr in `listen_addrs`, which is trivially satisfied. The attack is repeatable: after the 5-minute `TIMEOUT` expires, the attacker can re-poison with a new `ConnectionRequest` and restart.

## Recommendation

1. **Add an explicit `from != to` guard** at the top of both `ConnectionRequestProcess::execute()` and `ConnectionSyncProcess::execute()`:

```rust
if content.from == content.to {
    return StatusCode::InvalidFromPeerId.with_context("from and to must be distinct peers");
}
```

2. **Consume the `pending_delivered` entry after use** in `ConnectionSyncProcess::execute()` — change `.get(&content.from)` to `.remove(&content.from)` so a single poisoned entry cannot be reused across multiple `ConnectionSync` messages. [3](#0-2) 

3. **Cap the number of concurrently spawned NAT traversal tasks** per peer to bound worst-case resource consumption even if other guards are bypassed.

## Proof of Concept

```
1. Connect to victim CKB node as a normal P2P peer (HolePunching protocol enabled).

2. Obtain victim's peer ID via the Identify protocol.

3. Send ConnectionRequest:
   - from         = victim_peer_id
   - to           = victim_peer_id   ← same as from
   - max_hops     = 6
   - route        = []               ← empty, bypasses route-loop check
   - listen_addrs = 24× "/ip4/<attacker_ip>/tcp/<port>/p2p/<victim_peer_id>"
                    (any reachable or unreachable TCP/IP addresses)

   Result: victim calls respond_delivered(victim_peer_id, ...),
   inserts pending_delivered[victim_peer_id] = ([24 attacker addrs], now),
   sends ConnectionRequestDelivered back to attacker.

4. Send ConnectionSync once per second for 30+ seconds:
   - from  = victim_peer_id
   - to    = victim_peer_id
   - route = []

   Each message: victim finds pending_delivered[victim_peer_id],
   spawns runtime::spawn task with select_ok(24 try_nat_traversal futures),
   each future loops for 30 seconds making TCP connects to attacker addresses.

5. After 30 seconds:
   30 concurrent background tasks × 24 concurrent TCP futures
   = 720 concurrent open sockets → file descriptor exhaustion → node DoS.

6. Attack sustains for 5 minutes (TIMEOUT) from a single ConnectionRequest.
   Re-send ConnectionRequest after 5 minutes to extend indefinitely.
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

**File:** network/src/protocols/hole_punching/mod.rs (L28-28)
```rust
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/mod.rs (L44-46)
```rust
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L62-111)
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
        )
        .await
        {
            Ok(Ok(stream)) => {
                // try get the stored error in the underlying socket
                // if the socket is not connected, it will return an error
                if let Err(err) = check_connection(&stream) {
                    debug!("Failed to connect to NAT(base check): {}", err);
                }
                return Ok((stream, addr));
            }
            Err(err) => {
                debug!("Failed to connect to NAT(timeout): {}", err);
            }
            Ok(Err(err)) => {
                if err.kind() == std::io::ErrorKind::AddrNotAvailable {
                    return Err(err);
                }
                debug!(
                    "Failed to connect to NAT(other error): {}, {}",
                    err.kind(),
                    err
                );
            }
        }
        runtime::delay_for(actual_interval).await;
    }
```

**File:** network/src/network.rs (L940-954)
```rust
        // HolePunching protocol
        #[cfg(not(target_family = "wasm"))]
        if config
            .support_protocols
            .contains(&SupportProtocol::HolePunching)
        {
            let hole_punching_state = Arc::clone(&network_state);
            let hole_punching_meta =
                SupportProtocols::HolePunching.build_meta_with_service_handle(move || {
                    ProtocolHandle::Callback(Box::new(
                        crate::protocols::hole_punching::HolePunching::new(hole_punching_state),
                    ))
                });
            protocol_metas.push(hole_punching_meta);
        }
```
