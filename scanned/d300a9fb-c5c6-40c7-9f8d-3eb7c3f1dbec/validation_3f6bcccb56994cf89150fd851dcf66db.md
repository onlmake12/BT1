Audit Report

## Title
Eviction Protection Bypass via Attacker-Controlled Ping Timestamps Enables Systematic Eclipse Attack — (`network/src/peer_registry.rs`)

## Summary

`PeerRegistry::try_evict_inbound_peer` protects peers from eviction using `last_ping_protocol_message_received_at`, which the code comments claim is "hard to simulate or manipulate." In reality, this field is directly set whenever any inbound peer sends an unsolicited `Ping` message, giving an attacker with 8 connections deterministic control over Pass 2 of the eviction algorithm. Combined with a `as_secs()` granularity bug that degrades Pass 1 (lowest-ping protection) to a random shuffle among all sub-second peers, an attacker can reliably ensure their connections are never evicted, leaving only honest peers as candidates for removal.

## Finding Description

**Root cause — two independent bugs compound:**

**Bug 1 — Pass 1 (`as_secs()` granularity):** [1](#0-0) 

The comparator maps `ping_rtt` to `p.as_secs()`. For any peer with sub-second RTT (virtually all internet peers), this returns `0`, making all comparisons `Equal`. Rust's stable sort preserves the original iteration order (HashMap order, effectively random) for equal elements. The "lowest ping" protection tier degrades to a random selection among all peers that have a measured RTT.

**Bug 2 — Pass 2 (attacker-controllable timestamp):** [2](#0-1) 

The comparator uses `last_ping_protocol_message_received_at` with `.as_secs()` granularity. This field is set unconditionally in `ping_received()`: [3](#0-2) 

There is no rate limit, no check that the ping was solicited, and no authentication. An attacker can send a `Ping` message at any time on any of their sessions. The `received()` handler dispatches to `ping_received()` without any guard: [4](#0-3) 

If the attacker sends a `Ping` on each of their 8 connections immediately before eviction is triggered, those connections get `duration_since = 0 seconds`. Honest peers whose last ping exchange was at the regular interval (e.g., 15 s ago) get `duration_since = 15`. The sort (`peer2_last_message.cmp(&peer1_last_message)`, descending) places the attacker's connections at the protected tail; `truncate` removes the honest peers from the front. All 8 attacker connections are protected and removed from the candidate list.

**Pass 3** protects half the remaining candidates by `connected_time`, which is set at `Peer::new()` and is not attacker-controllable: [5](#0-4) 

After all three passes, the candidate pool consists entirely of honest peers. The final step groups by network group and randomly evicts one — always an honest peer.

**Existing checks reviewed and found insufficient:**

- The pong nonce check (`(true, nonce) == (status.processing, status.nonce())`) only applies to `Pong` messages, not `Ping` messages. Unsolicited pings are accepted unconditionally.
- The network group diversity step at the end only affects which honest peer is evicted; it does not protect honest peers from being the sole eviction candidates.
- The whitelist check (`!peer.is_whitelist`) only excludes whitelisted peers from the candidate pool; it does not protect non-whitelisted honest peers.

## Impact Explanation

Each eviction round removes one honest peer and admits one new attacker connection. Repeated over `max_inbound` rounds, the attacker fills all inbound slots with attacker-controlled peers, achieving a full eclipse. An eclipsed node receives only attacker-curated blocks and transactions, directly enabling **consensus deviation** (feeding a minority/forked chain) and **double-spend facilitation**. This matches the Critical impact class: "Vulnerabilities which could easily cause consensus deviation" (15001–25000 points).

## Likelihood Explanation

- **Attacker cost**: 8 persistent inbound connections from distinct IPs (achievable from a single host with multiple IPs or a small VPS cluster). No privileged access, no authentication, pure P2P inbound path.
- **Pass 2 is fully deterministic**: Sending a single `Ping` packet per connection immediately before eviction is triggered sets `duration_since = 0 s` with certainty. The attacker controls the exact timestamp.
- **Pass 1 is probabilistic but not required**: Even if the attacker does not occupy any Pass 1 slots, Pass 2 alone is sufficient to protect all 8 attacker connections.
- **Repeatability**: Each eviction round requires only one new connection attempt. The attack scales linearly with `max_inbound`.
- **No victim action required**: The victim node's normal operation (accepting inbound connections, running the ping protocol) is sufficient to trigger the vulnerability.

## Recommendation

1. **Fix `as_secs()` → use sub-second granularity** in both ping comparators in `try_evict_inbound_peer`. Replace `.as_secs()` with `.as_millis()` or `.as_nanos()` so the lowest-RTT protection is meaningful and not a random shuffle.
2. **Do not use attacker-sendable messages as protection criteria.** `last_ping_protocol_message_received_at` is updated on receipt of a peer-initiated `Ping`. Replace this field with a timestamp that only the local node controls — for example, only update on a valid `Pong` response to a node-initiated ping (i.e., update only in `pong_received`, not in `ping_received`).
3. **Rate-limit unsolicited inbound `Ping` messages** to prevent timestamp-freshening attacks even if the field semantics are corrected.
4. **Add IP subnet diversity enforcement** to the protection tiers so that multiple connections from the same /16 cannot all occupy protected slots simultaneously.

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
        Immediately before this, attacker sends one Ping message on each of A1..A8.
        → last_ping_protocol_message_received_at[Ai] = Instant::now()
        → duration_since[Ai] = 0 s; duration_since[Hi] = 15 s

Step 4: try_evict_inbound_peer() runs:
          Pass 1: All sub-second peers tie at 0; random 8 protected (attacker may get some).
          Pass 2: A1..A8 have duration=0; honest peers have duration=15.
                  Descending sort places A1..A8 at the tail → protected.
                  Honest peers are at the front → truncated (removed from candidates).
          Pass 3: Half of remaining honest peers protected by connection age.
          Network group step: largest group of honest peers selected.
          → One honest peer Hx is evicted.

Step 5: Repeat Step 3-4 (N-8) times.
        → All honest peers evicted; all N slots held by attacker.
        → Node is fully eclipsed.

Invariant violated:
  The comment "characteristics that an attacker hard to simulate or manipulate"
  is false for last_ping_protocol_message_received_at, which is set by
  ping_received() on every unsolicited inbound Ping with no guard.
```

### Citations

**File:** network/src/peer_registry.rs (L151-165)
```rust
        sort_then_drop(
            &mut candidate_peers,
            EVICTION_PROTECT_PEERS,
            |peer1, peer2| {
                let peer1_ping = peer1
                    .ping_rtt
                    .map(|p| p.as_secs())
                    .unwrap_or_else(|| u64::MAX);
                let peer2_ping = peer2
                    .ping_rtt
                    .map(|p| p.as_secs())
                    .unwrap_or_else(|| u64::MAX);
                peer2_ping.cmp(&peer1_ping)
            },
        );
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
