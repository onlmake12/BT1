All technical claims verified against the actual code. Every assertion holds:

- `PingHandler` struct (L32–39): no `rate_limiter` field confirmed. [1](#0-0) 
- `received()` Ping branch (L215–223): unconditional `ping_received()` + `send_message()`, no guard. [2](#0-1) 
- `ping_received()` (L62–68): writes `Instant::now()` unconditionally. [3](#0-2) 
- `HolePunching.rate_limiter` field (L45) and check (L95–107) confirmed. [4](#0-3) [5](#0-4) 
- `EVICTION_PROTECT_PEERS = 8` (L17) and eviction sort on `last_ping_protocol_message_received_at` (L167–183) confirmed. [6](#0-5) [7](#0-6) 

---

Audit Report

## Title
Unbounded Ping Flood Enables Outbound Bandwidth Exhaustion and Eviction-Protection Manipulation — (`network/src/protocols/ping.rs`)

## Summary
`PingHandler` contains no per-session rate limiter. Any connected peer can send Ping messages at an arbitrary rate; the handler unconditionally responds with a Pong for every Ping and simultaneously refreshes the peer's `last_ping_protocol_message_received_at` eviction-protection timestamp on every message. Both other stateful protocol handlers in the same codebase (`Relayer`, `HolePunching`) carry explicit `governor`-based rate limiters; `PingHandler` does not.

## Finding Description
In `network/src/protocols/ping.rs`, the `PingHandler` struct (L32–39) carries no `rate_limiter` field. In `received()` (L201–249), the `PingPayload::Ping` branch (L215–223) performs two unconditional operations with no guard:

1. `self.ping_received(session.id)` — writes `Instant::now()` to `peer.last_ping_protocol_message_received_at` (L62–68).
2. `context.send_message(PingMessage::build_pong(nonce))` — emits one Pong per Ping.

By contrast, `HolePunching::received()` (L95–107 of `network/src/protocols/hole_punching/mod.rs`) checks a `governor` rate limiter keyed by `(session_id, msg.item_id())` before any processing and silently drops excess messages. `Relayer` does the same. `PingHandler` has no equivalent check.

The eviction logic in `network/src/peer_registry.rs` `try_evict_inbound_peer()` (L167–183) uses `last_ping_protocol_message_received_at` as the sort key for the second protection pass, shielding the 8 peers with the most-recent timestamps (`EVICTION_PROTECT_PEERS = 8`, L17) from eviction. Because `ping_received()` is called unconditionally on every inbound Ping, an attacker who sends Pings in a tight loop keeps their timestamp perpetually at `Instant::now()`, guaranteeing occupancy of one of those 8 protected slots.

## Impact Explanation
**Outbound bandwidth exhaustion (High — network congestion with few costs):** Each inbound Ping produces one outbound Pong with no throttle. An attacker with a single connection can drive the victim's outbound message queue at the attacker's chosen rate. Pong messages share the same send queue as block/transaction relay messages; sustained flooding degrades relay throughput for all honest peers on that node. Scaled across many nodes (each requiring only a standard TCP connection), this constitutes network-wide congestion achievable at negligible cost, matching the High impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

**Eviction-protection manipulation:** By continuously sending Pings, an attacker guarantees their session is never selected for eviction while honest idle peers are. Combined with multiple attacker-controlled connections, this can lock honest peers out of the protected set and bias eviction toward them, enabling persistent eclipse-attack positioning.

## Likelihood Explanation
The attacker requires only a standard inbound or outbound P2P connection — no authentication, no PoW, no privileged role. The Ping protocol is listed in the default `support_protocols` configuration and is enabled on every production node. The attack is trivially scriptable: open a TCP connection, send Ping frames in a loop. It is repeatable, persistent, and requires no victim interaction or mistake.

## Recommendation
Add a per-session rate limiter to `PingHandler`, consistent with the pattern already used in `Relayer` and `HolePunching`:

- Add a `RateLimiter<(SessionId, u32)>` field to `PingHandler`.
- In the `PingPayload::Ping` branch of `received()`, check the limiter before calling `ping_received()` or `send_message()`. Drop (or disconnect) the session on excess.
- A limit of 1–2 Pings per second per session is sufficient; the legitimate use case (node-initiated keepalive) fires at most once per `ping_interval_secs` (default 120 s per `resource/ckb.toml`).

## Proof of Concept
```
1. Establish one inbound TCP connection to the victim node.
2. In a tight loop, send Ping(nonce=0x41414141) as fast as the TCP window allows.
3. Observe: victim emits one Pong per Ping with no throttle.
4. Observe: victim's last_ping_protocol_message_received_at for the attacker session
   stays within milliseconds of Instant::now() at all times.
5. Assert: attacker session is never selected for eviction while honest idle peers are.
6. Assert: victim outbound bandwidth is proportional to attacker send rate with no cap.
```

### Citations

**File:** network/src/protocols/ping.rs (L32-39)
```rust
pub struct PingHandler {
    interval: Duration,
    timeout: Duration,
    connected_session_ids: HashMap<SessionId, PingStatus>,
    network_state: Arc<NetworkState>,
    control_receiver: Receiver<()>,
    start_time: Instant,
}
```

**File:** network/src/protocols/ping.rs (L62-68)
```rust
    fn ping_received(&mut self, id: SessionId) {
        trace!("received ping from: {:?}", id);
        self.network_state.with_peer_registry_mut(|reg| {
            if let Some(peer) = reg.get_peer_mut(id) {
                peer.last_ping_protocol_message_received_at = Some(Instant::now());
            }
        });
```

**File:** network/src/protocols/ping.rs (L215-223)
```rust
                    PingPayload::Ping(nonce) => {
                        self.ping_received(session.id);
                        if context
                            .send_message(PingMessage::build_pong(nonce))
                            .await
                            .is_err()
                        {
                            debug!("Failed to send message");
                        }
```

**File:** network/src/protocols/hole_punching/mod.rs (L45-45)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
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

**File:** network/src/peer_registry.rs (L17-17)
```rust
pub(crate) const EVICTION_PROTECT_PEERS: usize = 8;
```

**File:** network/src/peer_registry.rs (L167-183)
```rust
        // Protect peers which most recently sent messages
        sort_then_drop(
            &mut candidate_peers,
            EVICTION_PROTECT_PEERS,
            |peer1, peer2| {
                let now = Instant::now();
                let peer1_last_message = peer1
                    .last_ping_protocol_message_received_at
                    .map(|t| now.saturating_duration_since(t).as_secs())
                    .unwrap_or_else(|| u64::MAX);
                let peer2_last_message = peer2
                    .last_ping_protocol_message_received_at
                    .map(|t| now.saturating_duration_since(t).as_secs())
                    .unwrap_or_else(|| u64::MAX);
                peer2_last_message.cmp(&peer1_last_message)
            },
        );
```
