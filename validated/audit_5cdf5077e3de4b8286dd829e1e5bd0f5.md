The code confirms all factual claims. Let me verify the relayer for completeness.

All code facts are confirmed. The vulnerability is valid.

Audit Report

## Title
Unbounded Ping Flood Enables Outbound Bandwidth Exhaustion and Eviction-Protection Manipulation — (`network/src/protocols/ping.rs`)

## Summary
`PingHandler` contains no per-session rate limiter. Any connected peer can send `Ping` messages at an arbitrary rate; the handler unconditionally responds with a `Pong` for every `Ping` and simultaneously refreshes the peer's `last_ping_protocol_message_received_at` eviction-protection timestamp on every message. Both `Relayer` and `HolePunching` carry explicit `governor`-based rate limiters at 30 req/s; `PingHandler` does not.

## Finding Description
In `received()`, the `PingPayload::Ping` branch at `network/src/protocols/ping.rs` lines 215–223 performs two unconditional operations with no guard:

1. `self.ping_received(session.id)` — writes `Instant::now()` to `peer.last_ping_protocol_message_received_at` (lines 62–68).
2. `context.send_message(PingMessage::build_pong(nonce))` — emits one Pong per Ping.

`PingHandler`'s struct definition (lines 32–39) has no `rate_limiter` field and no frequency check is performed anywhere in `received()`.

By contrast, `Relayer::new()` at `sync/src/relayer/mod.rs` lines 91–92 installs a `governor` rate limiter capped at 30 req/s per `(peer, message_type)` key and checks it before every message (lines 116–123). `HolePunching` does the same at `network/src/protocols/hole_punching/mod.rs` lines 95–107.

The eviction logic in `network/src/peer_registry.rs` lines 167–183 uses `last_ping_protocol_message_received_at` to protect up to `EVICTION_PROTECT_PEERS = 8` inbound peers from eviction. An attacker flooding Pings keeps this timestamp perpetually at `Instant::now()`, guaranteeing one of those 8 protected slots.

## Impact Explanation
**Outbound bandwidth exhaustion**: Each inbound `Ping` produces one outbound `Pong` with no throttle. A single attacker connection can drive the victim's outbound message queue at the attacker's chosen rate. Pong messages share the same send queue as block/transaction relay messages; sustained flooding degrades relay throughput for all honest peers on that node. Scaled across multiple attacker-controlled connections targeting multiple nodes, this constitutes a low-cost mechanism to cause CKB network congestion — matching the **High** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

**Eviction-protection manipulation**: The attacker permanently occupies one of the 8 eviction-protected slots, biasing eviction toward honest idle peers and facilitating eclipse attack preconditions.

## Likelihood Explanation
The attacker requires only a standard inbound TCP connection. No authentication, PoW, or privileged role is needed. `Ping` is listed in the default `support_protocols` in `resource/ckb.toml` line 112 and is enabled on every production node. The legitimate ping interval is 120 seconds (`ping_interval_secs = 120`, line 97); an attacker can send thousands of Pings per second over the same connection. The attack is repeatable, requires no victim mistake, and is trivially automatable.

## Recommendation
Add a per-session rate limiter to `PingHandler`, consistent with the pattern already used in `Relayer` and `HolePunching`:

- Add a `RateLimiter<(SessionId, u32)>` field to `PingHandler`.
- In the `PingPayload::Ping` branch of `received()`, check the limiter before calling `ping_received()` or `send_message()`. Drop or disconnect the session on excess.
- A limit of 1–2 Pings per second per session is sufficient; the legitimate use case fires at most once per `ping_interval_secs` (default 120 s).

## Proof of Concept
```
1. Establish one inbound TCP connection to the victim node.
2. In a tight loop, send Ping(nonce=0x41414141) as fast as the TCP window allows.
3. Observe: victim emits one Pong per Ping with no throttle (confirmed by
   network/src/protocols/ping.rs lines 215-223).
4. Observe: victim's last_ping_protocol_message_received_at for the attacker
   session stays within milliseconds of Instant::now() at all times
   (network/src/protocols/ping.rs lines 62-68).
5. Assert: attacker session is never selected for eviction while honest idle
   peers are (network/src/peer_registry.rs lines 167-183).
6. Assert: victim outbound bandwidth is proportional to attacker send rate
   with no application-layer cap.
```