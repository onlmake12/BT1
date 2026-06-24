Audit Report

## Title
Missing `from != to` Validation Enables Resource Exhaustion via Self-Addressed Hole-Punching — (`network/src/protocols/hole_punching/component/connection_request.rs`)

## Summary

An unprivileged connected peer can send a `ConnectionRequest` with `from == to == victim_node_peer_id` and attacker-controlled TCP addresses in `listen_addrs`. Because no guard validates that `from != to`, the victim node inserts a `pending_delivered` entry keyed by its own peer ID. Subsequent `ConnectionSync` messages with the same self-referential IDs cause the node to repeatedly spawn 30-second background tasks, each running up to 24 concurrent TCP connection futures to attacker-controlled addresses. The rate limiter permits 1 such `ConnectionSync` per second; after 30 seconds, up to 30 concurrent tasks × 24 concurrent TCP futures are active, approaching file-descriptor limits and causing sustained resource exhaustion.

## Finding Description

**Root cause — no `from != to` guard anywhere in the hole-punching handlers.**

**Step 1 — Poison `pending_delivered` via `ConnectionRequest` with `from == to == self_peer_id`:**

In `ConnectionRequestProcess::execute()`:
- Line 128: `content.route.contains(self_peer_id)` — passes with an empty route.
- Lines 132–143: `forward_rate_limiter` keyed by `(from, to, msg_item_id)` — 1 req/sec, passes on first call.
- Line 145: `self_peer_id == &content.to` — **TRUE** when `to == self_peer_id`, so `respond_delivered` is called with `from_peer_id = self_peer_id`.

Inside `respond_delivered` (lines 161–237):
- Line 161: Checks for an existing entry within `HOLE_PUNCHING_INTERVAL` (2 min) — passes on first call.
- Lines 196–215: Attacker-supplied `listen_addrs` are filtered to TCP/IP-only addresses.
- Line 217: Returns `Ignore` only if the filtered list is empty — passes if attacker provides valid TCP/IP addresses.
- Lines 226–232: Sends `ConnectionRequestDelivered` back to the attacker's session.
- Lines 234–237: **Inserts `pending_delivered[self_peer_id] = (attacker_tcp_addrs, now)`.**

There is no check that `content.from` matches the actual sender's peer ID, and no check that `from != to`.

**Step 2 — Trigger NAT traversal via `ConnectionSync` with `from == to == self_peer_id`, empty route:**

In `ConnectionSyncProcess::execute()`:
- Line 82: Empty route passes the length check.
- Lines 85–96: `forward_rate_limiter` keyed by `(self_peer_id, self_peer_id, msg_item_id)` — 1 req/sec.
- Line 98: `content.route.last()` is `None` (empty route) → enters the `None` branch.
- Line 102: `self_peer_id != &content.to` — **FALSE** → enters the "current node is the `to` target" branch.
- Lines 111–115: `pending_delivered.get(&content.from)` where `content.from == self_peer_id` → **finds the poisoned entry**.
- Lines 119–124: Creates `try_nat_traversal` futures for each stored address (up to `ADDRS_COUNT_LIMIT = 24`).
- Lines 135–162: If the node has a listen address configured (typical for any running CKB node), **spawns an async task** running `select_ok(tasks)` — all 24 futures run concurrently for up to 30 seconds.

The `pending_delivered` entry is only `.get()`-read, **never removed after use**, so it persists for `TIMEOUT = 5 minutes`.

**`try_nat_traversal` resource cost per task:**

Each future loops for up to 30 seconds (`timeout_duration = Duration::from_secs(30)`), creating a new TCP socket and attempting a connect with a 200ms timeout, then sleeping ~200ms before the next attempt. At any given moment, each future holds one open socket during the connect phase. With 24 futures running concurrently via `select_ok`, each spawned task holds up to 24 open sockets simultaneously during connect phases.

**Rate limiter analysis:**

`forward_rate_limiter` is keyed by `(from, to, msg_item_id)` at 1 req/sec. With `from == to == self_peer_id` and a fixed `msg_item_id`, the attacker is limited to 1 `ConnectionSync` per second. However, each invocation spawns a **30-second** background task. After 30 seconds: **30 concurrent tasks × 24 concurrent TCP futures ≈ up to 720 concurrent open sockets** (approximately half active at any moment due to the connect/sleep cycle, still ~360 concurrent sockets sustained).

## Impact Explanation

**High — Vulnerability which could easily crash a CKB node.**

- **File descriptor exhaustion**: Hundreds of concurrent TCP sockets approach the typical non-root process fd limit (1024). Once exhausted, the node cannot accept new P2P connections, open database files, or perform any fd-requiring operation — effective DoS.
- **CPU/async runtime pressure**: Dozens of concurrent tokio tasks continuously polling TCP futures.
- **SSRF-like outbound connections**: The node makes TCP connections to arbitrary attacker-controlled IP:port combinations, enabling internal network port scanning.
- **Sustained attack window**: The `pending_delivered` entry persists for 5 minutes, allowing the attacker to sustain the attack without re-sending the initial `ConnectionRequest`.

## Likelihood Explanation

The attacker only needs to be a connected P2P peer (no special privileges). The `HolePunching` protocol is enabled by default when `SupportProtocol::HolePunching` is in the config. The victim's peer ID is publicly available via the Identify protocol. The two-message sequence (`ConnectionRequest` then `ConnectionSync`) is straightforward to craft. The only precondition is supplying at least one valid TCP/IP multiaddr in `listen_addrs`, which is trivially satisfied. The attack is repeatable every 5 minutes (when the `pending_delivered` entry expires) with a single re-poisoning `ConnectionRequest`.

## Recommendation

1. Add an explicit `from != to` guard at the top of both `ConnectionRequestProcess::execute()` and `ConnectionSyncProcess::execute()`:

```rust
if content.from == content.to {
    return StatusCode::InvalidFromPeerId.with_context("from and to must be distinct peers");
}
```

2. Verify that `content.from` matches the actual sender's peer ID (obtainable from the session's peer registry) in `ConnectionRequestProcess::execute()`, preventing spoofed `from` fields entirely.

3. Change `.get(&content.from)` to `.remove(&content.from)` in `ConnectionSyncProcess::execute()` so the `pending_delivered` entry is consumed after use, preventing repeated triggering from a single poisoned entry.

## Proof of Concept

```
1. Connect to victim CKB node as a normal P2P peer (HolePunching protocol).

2. Obtain victim's peer ID (available via Identify protocol).

3. Send ConnectionRequest:
   - from  = victim_peer_id
   - to    = victim_peer_id   ← same as from
   - max_hops = 6
   - route = []               ← empty, bypasses route-loop check
   - listen_addrs = ["/ip4/192.168.1.1/tcp/8115/p2p/<victim_peer_id>"]
     (any valid TCP/IP address; attacker controls this target)

   Result: victim calls respond_delivered(victim_peer_id, ...),
   inserts pending_delivered[victim_peer_id] = ([192.168.1.1:8115], now),
   sends ConnectionRequestDelivered back to attacker.

4. Send ConnectionSync once per second for 30+ seconds:
   - from  = victim_peer_id
   - to    = victim_peer_id
   - route = []

   Each message: victim finds pending_delivered[victim_peer_id],
   spawns async task with up to 24 concurrent try_nat_traversal futures,
   each looping for 30 seconds making TCP connects to 192.168.1.1:8115.

5. After 30 seconds: 30 concurrent background tasks × 24 TCP futures
   = up to 720 concurrent open sockets → file descriptor exhaustion → node DoS.

6. Attack sustains for 5 minutes (TIMEOUT) without re-sending ConnectionRequest.
   Re-send after 2 minutes (HOLE_PUNCHING_INTERVAL) to refresh the entry.
```

**Relevant code references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** network/src/protocols/hole_punching/component/connection_request.rs (L127-153)
```rust
        let self_peer_id = self.protocol.network_state.local_peer_id();
        if content.route.contains(self_peer_id) {
            return StatusCode::Ignore.with_context("the message is passed, ignore it");
        }

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

        if self_peer_id == &content.to {
            self.respond_delivered(content.from, &content.to, content.listen_addrs)
                .await
        } else if content.max_hops == 0u8 {
            StatusCode::ReachedMaxHops.into()
        } else {
            self.forward_message(self_peer_id, &content.to).await
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

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L98-162)
```rust
        match content.route.last() {
            Some(next_peer_id) => self.forward_sync(next_peer_id).await,
            None => {
                let self_peer_id = self.protocol.network_state.local_peer_id();
                if self_peer_id != &content.to {
                    // forward the message to the `to` peer
                    self.forward_sync(&content.to).await
                } else {
                    // Current node should be the `to` target.
                    if let Some(metrics) = ckb_metrics::handle() {
                        metrics.ckb_hole_punching_passive_count.inc();
                    }

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

**File:** network/src/protocols/hole_punching/mod.rs (L27-28)
```rust
const ADDRS_COUNT_LIMIT: usize = 24;
const TIMEOUT: u64 = 5 * 60 * 1000; // 5 minutes
```

**File:** network/src/protocols/hole_punching/component/mod.rs (L62-115)
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

    debug!("Failed to connect to NAT after {} retries", retry_count);
    Err(std::io::ErrorKind::TimedOut.into())
}
```
