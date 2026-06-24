The code confirms the claim. Let me verify the key facts:

1. **`pending_delivered` is read but not removed** in `connection_sync.rs` lines 111–115: only `.get()` is called, no `.remove()` or `.clear()` follows after spawning the task. [1](#0-0) 

2. **The entry only expires via periodic `notify()` cleanup** after `TIMEOUT = 5 * 60 * 1000` ms. [2](#0-1) 

3. **`forward_rate_limiter` is 1/second** per `(from, to, msg_item_id)` — since `msg_item_id` is fixed for `ConnectionSync`, this is 1/second per `(from, to)` pair. [3](#0-2) 

4. **Each `runtime::spawn` is unconditional** — no deduplication or in-flight task tracking exists. [4](#0-3) 

5. **`respond_delivered` cooldown does NOT protect `ConnectionSync`** — it only gates re-responding to `ConnectionRequest`, not re-triggering NAT traversal. [5](#0-4) 

The claim is accurate and the exploit path is reachable by any connected peer. The impact (resource exhaustion via unbounded task spawning) maps to High: "Vulnerabilities which could easily crash a CKB node."

---

Audit Report

## Title
`pending_delivered` Entry Not Consumed After `ConnectionSync` Processing Allows Repeated NAT Traversal Task Spawning - (File: `network/src/protocols/hole_punching/component/connection_sync.rs`)

## Summary
In `ConnectionSyncProcess::execute`, when the target node processes a `ConnectionSync` message, it reads the attacker's listen addresses from `pending_delivered` and unconditionally spawns a `try_nat_traversal` task without removing or marking the entry as consumed. Because the entry persists for the full 5-minute `TIMEOUT` window and the only throttle is a 1-request/second rate limiter, an attacker can trigger up to 300 concurrent NAT traversal tasks per `(from, to)` pair per window, each making ~150 outbound TCP connection attempts over 30 seconds, exhausting file descriptors and connection capacity on the victim node.

## Finding Description
`HolePunching` maintains `pending_delivered: HashMap<PeerId, PendingDeliveredInfo>` where `PendingDeliveredInfo = (Vec<Multiaddr>, u64)`. This map is populated in `connection_request.rs::respond_delivered` (line 237) when the target node responds to a `ConnectionRequest`, storing the attacker's listen addresses and a timestamp.

In `connection_sync.rs::execute` (lines 111–115), when the target node is the `to` peer, it reads the entry with `.get(&content.from)` and clones the listen addresses to build NAT traversal tasks. After `runtime::spawn` is called (line 145), the entry is **never removed, cleared, or flagged**. The entry only expires via the periodic `notify()` cleanup at `TIMEOUT = 5 * 60 * 1000` ms (lines 173–174 of `mod.rs`).

The sole rate-limiting protection is `forward_rate_limiter` keyed on `(content.from, content.to, self.msg_item_id)` at 1 request/second (lines 85–96 of `connection_sync.rs`, configured at lines 255–257 of `mod.rs`). Since `msg_item_id` is the fixed `ConnectionSync` message type discriminant, this permits exactly 1 new `runtime::spawn` per second for the full 5-minute window — 300 spawned tasks total.

The `respond_delivered` cooldown check (lines 161–167 of `connection_request.rs`) only gates re-responding to `ConnectionRequest` messages; it provides no protection against repeated `ConnectionSync` processing.

**Exploit flow:**
1. Attacker connects to target node T and sends `ConnectionRequest { from=A, to=T, listen_addrs=[...] }`.
2. T calls `respond_delivered`, stores `pending_delivered[A] = ([listen_addrs], now)`, sends `ConnectionRequestDelivered` back.
3. Attacker sends `ConnectionSync { from=A, to=T }` once per second for 5 minutes.
4. Each message passes the rate limiter, reads `pending_delivered[A]` (entry still present), and spawns a new `try_nat_traversal` task.
5. Each task runs for up to 30 seconds, making TCP connection attempts every ~200 ms (~150 attempts/task).
6. At peak, ~30 concurrent tasks are active, generating ~4,500 outbound TCP attempts in flight.

## Impact Explanation
This is a **High** severity vulnerability matching the impact class: *"Vulnerabilities which could easily crash a CKB node."*

The unbounded spawning of `try_nat_traversal` tasks exhausts OS-level file descriptors (each TCP `connect()` attempt consumes one), CPU (async task scheduling and socket I/O), and outbound connection capacity. A single attacker with one direct P2P connection can sustain this attack indefinitely by re-triggering every 5 minutes as the `pending_delivered` entry expires and is re-created via a new `ConnectionRequest`. Under sustained attack, the node becomes unable to accept new connections or process existing ones, constituting a practical crash/denial-of-service.

## Likelihood Explanation
The attack requires only: (1) a direct P2P connection to the target node, and (2) the ability to send `HolePunching` protocol messages — both available to any unprivileged peer on the network. No special keys, privileges, or majority hashpower are needed. The `ConnectionRequest` step is trivially satisfied by any connected peer. The attack is repeatable every 5 minutes and is fully automatable.

## Recommendation
After spawning the NAT traversal task in `connection_sync.rs`, consume the listen addresses from the `pending_delivered` entry to prevent re-triggering while preserving the timestamp for the `respond_delivered` cooldown:

```rust
// After runtime::spawn(...):
if let Some(entry) = self.protocol.pending_delivered.get_mut(&content.from) {
    entry.0.clear(); // Consume listen addresses; keep timestamp for cooldown
}
```

Alternatively, maintain a separate `HashSet<PeerId>` tracking peers for which NAT traversal has already been initiated within the current session, and skip re-spawning if the peer is already present. The set should be cleared alongside `pending_delivered` in `notify()`.

## Proof of Concept
1. Establish a direct P2P connection to target node T as peer A.
2. Send `HolePunching::ConnectionRequest { from=A, to=T, listen_addrs=[<valid TCP addr>], max_hops=6, route=[] }`.
3. Observe `ConnectionRequestDelivered` response; confirm `pending_delivered[A]` is now set.
4. In a loop, send `HolePunching::ConnectionSync { from=A, to=T, route=[] }` once per second for 300 seconds.
5. Monitor target node's open file descriptor count (`/proc/<pid>/fd`) and CPU usage — both will increase monotonically as `try_nat_traversal` tasks accumulate.
6. After ~30 seconds of attack, observe ~30 concurrent tasks each consuming file descriptors for TCP `connect()` calls to the attacker-supplied address.
7. Confirm node degradation (failed connections, increased latency, or crash) as file descriptor limit is approached.

### Citations

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L111-115)
```rust
                    let listens_info = self
                        .protocol
                        .pending_delivered
                        .get(&content.from)
                        .map(|info| info.0.clone());
```

**File:** network/src/protocols/hole_punching/component/connection_sync.rs (L143-163)
```rust
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
                                    Status::ok()
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
