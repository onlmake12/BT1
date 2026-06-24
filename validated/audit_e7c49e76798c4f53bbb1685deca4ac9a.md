Audit Report

## Title
Eviction Protection Bypass via Attacker-Controlled Ping Timestamps Enables Systematic Eclipse Attack — (`network/src/peer_registry.rs`)

## Summary

`PeerRegistry::try_evict_inbound_peer` uses `last_ping_protocol_message_received_at` as a Pass 2 eviction protection criterion, described in comments as "hard to simulate or manipulate." This field is set unconditionally in `ping_received()` whenever any inbound peer sends a `Ping` message, with no rate limit or solicitation check. An attacker with 8 inbound connections can deterministically protect all of them from eviction by sending a `Ping` on each connection immediately before eviction is triggered, ensuring only honest peers are evicted. A compounding `as_secs()` granularity bug in Pass 1 degrades lowest-ping protection to a random shuffle. Repeated eviction rounds allow a full eclipse of the node's inbound slots.

## Finding Description

**Bug 1 — Pass 1 (`as_secs()` granularity):**

The Pass 1 comparator maps `ping_rtt` to `p.as_secs()`. [1](#0-0) 

For any peer with sub-second RTT (virtually all internet peers), `p.as_secs()` returns `0`. All such peers compare `Equal`, and Rust's stable sort preserves original HashMap iteration order (effectively random) for equal elements. Pass 1 "lowest ping" protection degrades to a random selection among all peers with a measured RTT — attacker peers are not disadvantaged.

**Bug 2 — Pass 2 (attacker-controllable timestamp):**

The Pass 2 comparator uses `last_ping_protocol_message_received_at` with `.as_secs()` granularity, sorted descending (smallest `duration_since` = most recently active = protected tail). [2](#0-1) 

This field is set unconditionally in `ping_received()`: [3](#0-2) 

The `received()` handler dispatches to `ping_received()` with no guard against unsolicited pings: [4](#0-3) 

There is no rate limit, no check that the ping was solicited, and no authentication. If the attacker sends a `Ping` on each of their 8 connections immediately before eviction is triggered, those connections get `duration_since = 0 s`. Honest peers whose last ping exchange was at the regular interval (e.g., 15 s ago) get `duration_since = 15`. The descending sort places attacker connections at the protected tail; `truncate` removes honest peers from the front. All 8 attacker connections are protected and removed from the candidate list.

**Pass 3** protects half the remaining candidates by `connected_time`, which is set at `Peer::new()` and is not attacker-controllable: [5](#0-4) 

After all three passes, the candidate pool consists entirely of honest peers. The final network-group step randomly evicts one honest peer.

**Existing checks reviewed and found insufficient:**

- The pong nonce check `(true, nonce) == (status.processing, status.nonce())` applies only to `Pong` messages. Unsolicited `Ping` messages are accepted unconditionally with no guard.
- The network group diversity step only affects which honest peer is evicted; it does not protect honest peers from being the sole candidates.
- The whitelist check `!peer.is_whitelist` only excludes whitelisted peers; it does not protect non-whitelisted honest peers.

`EVICTION_PROTECT_PEERS` is defined as `8`: [6](#0-5) 

## Impact Explanation

Each eviction round removes one honest peer and admits one new attacker connection. Repeated over `max_inbound` rounds, the attacker fills all inbound slots with attacker-controlled peers, achieving a full eclipse. An eclipsed node receives only attacker-curated blocks and transactions, directly enabling **consensus deviation** (feeding a minority or forked chain) and double-spend facilitation. This matches the Critical impact class: "Vulnerabilities which could easily cause consensus deviation" (15001–25000 points).

## Likelihood Explanation

- **Attacker cost**: 8 persistent inbound connections from distinct IPs, achievable from a single host with multiple IPs or a small VPS cluster. No privileged access required.
- **Pass 2 is fully deterministic**: Sending one `Ping` packet per connection immediately before eviction sets `duration_since = 0 s` with certainty. The attacker controls the exact timestamp.
- **Pass 1 is not required**: Even without occupying any Pass 1 slots, Pass 2 alone is sufficient to protect all 8 attacker connections.
- **Repeatability**: Each eviction round requires only one new connection attempt. The attack scales linearly with `max_inbound`.
- **No victim action required**: The victim node's normal operation (accepting inbound connections, running the ping protocol) is sufficient to trigger the vulnerability.

## Recommendation

1. **Fix `as_secs()` granularity** in both ping comparators in `try_evict_inbound_peer`. Replace `.as_secs()` with `.as_millis()` or `.as_nanos()` so lowest-RTT protection is meaningful and not a random shuffle.
2. **Do not use attacker-sendable messages as protection criteria.** Replace `last_ping_protocol_message_received_at` with a timestamp that only the local node controls — update it only in `pong_received()` (on a valid `Pong` response to a node-initiated ping), not in `ping_received()`.
3. **Rate-limit unsolicited inbound `Ping` messages** to prevent timestamp-freshening attacks even if field semantics are corrected.
4. **Add IP subnet diversity enforcement** to protection tiers so that multiple connections from the same /16 cannot all occupy protected slots simultaneously.

## Proof of Concept

```
Setup:
  max_inbound = N (e.g., 117)
  EVICTION_PROTECT_PEERS = 8
  Ping interval = 15 s

Step 1: Attacker opens 8 inbound connections (A1..A8) from distinct IPs.
        Each connection responds to node-initiated pings immediately.
        → ping_rtt[Ai] ≈ 0 ms → as_secs() = 0 (same as all honest peers)

Step 2: Honest peers H1..H(N-8) fill remaining inbound slots.
        Their last_ping_protocol_message_received_at is ~15 s old.

Step 3: Attacker opens connection A9.
        accept_peer() detects non_whitelist_inbound >= max_inbound.
        Immediately before this, attacker sends one Ping on each of A1..A8.
        → last_ping_protocol_message_received_at[Ai] = Instant::now()
        → duration_since[Ai] = 0 s; duration_since[Hi] = 15 s

Step 4: try_evict_inbound_peer() runs:
          Pass 1: All sub-second peers tie at 0; random 8 protected.
          Pass 2: A1..A8 have duration=0; honest peers have duration=15.
                  Descending sort places A1..A8 at tail → protected.
                  Honest peers at front → truncated (removed from candidates).
          Pass 3: Half of remaining honest peers protected by connection age.
          Network group step: largest group of honest peers selected.
          → One honest peer Hx is evicted.

Step 5: Repeat Steps 3-4 (N-8) times.
        → All honest peers evicted; all N slots held by attacker.
        → Node is fully eclipsed.

Invariant violated:
  The comment "characteristics that an attacker hard to simulate or manipulate"
  is false for last_ping_protocol_message_received_at, which is set by
  ping_received() on every unsolicited inbound Ping with no guard.
```

### Citations

**File:** network/src/peer_registry.rs (L17-17)
```rust
pub(crate) const EVICTION_PROTECT_PEERS: usize = 8;
```

**File:** network/src/peer_registry.rs (L155-163)
```rust
                let peer1_ping = peer1
                    .ping_rtt
                    .map(|p| p.as_secs())
                    .unwrap_or_else(|| u64::MAX);
                let peer2_ping = peer2
                    .ping_rtt
                    .map(|p| p.as_secs())
                    .unwrap_or_else(|| u64::MAX);
                peer2_ping.cmp(&peer1_ping)
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

**File:** network/src/protocols/ping.rs (L214-223)
```rust
                match msg {
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

**File:** network/src/peer.rs (L104-104)
```rust
            connected_time: Instant::now(),
```
