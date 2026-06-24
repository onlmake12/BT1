The code confirms all claims. Let me verify the `sort_then_drop` semantics precisely before writing the report.

`sort_then_drop` at lines 55-63 of `peer_registry.rs`:
- Sorts the list by the comparator
- `truncate(list.len() - n)` keeps the **first** `list.len() - n` elements and removes the **last** `n`

Round 1 comparator: `peer2_ping.cmp(&peer1_ping)` → descending (largest first). After sort: `[u64::MAX, ..., best_ping]`. Truncate removes the last `n` (best ping) → **protects peers with lowest RTT**. Attacker with `ping_rtt = None` → `u64::MAX` is at the front, NOT protected, stays in candidate pool.

Round 2 comparator: `peer2_last_message.cmp(&peer1_last_message)` → descending (largest elapsed first). After sort: `[u64::MAX (no activity), ..., ~0 (most recent)]`. Truncate removes the last `n` (most recent) → **protects peers with most recent `last_ping_protocol_message_received_at`**. Attacker flooding Pings has elapsed ≈ 0 → always in the protected tail.

All code references check out exactly. The vulnerability is real.

---

Audit Report

## Title
Attacker-Controlled `last_ping_protocol_message_received_at` via Unsolicited Ping Flooding Bypasses Inbound Peer Eviction Protection — (`network/src/protocols/ping.rs`, `network/src/peer_registry.rs`)

## Summary
`ping_received()` unconditionally writes `Instant::now()` into `last_ping_protocol_message_received_at` for every incoming Ping message with no rate limit or authenticity requirement. `try_evict_inbound_peer()` uses this same field to protect the 8 most recently active peers from eviction. An attacker who floods unsolicited Ping messages keeps their elapsed time at ≈ 0 seconds and is permanently shielded from eviction, allowing them to monopolize inbound peer slots and displace legitimate peers.

## Finding Description

**Root cause — `ping_received()` (`ping.rs` lines 62–69):**

```rust
fn ping_received(&mut self, id: SessionId) {
    trace!("received ping from: {:?}", id);
    self.network_state.with_peer_registry_mut(|reg| {
        if let Some(peer) = reg.get_peer_mut(id) {
            peer.last_ping_protocol_message_received_at = Some(Instant::now());
        }
    });
}
```

Called unconditionally from the `received()` handler on every `PingPayload::Ping` (`ping.rs` lines 215–216) with no rate limit, nonce validation, or cost imposed on the sender.

**Eviction logic — `try_evict_inbound_peer()` (`peer_registry.rs` lines 167–183):**

The second protection round in `sort_then_drop` uses comparator `peer2_last_message.cmp(&peer1_last_message)` (descending: largest elapsed first). `truncate(list.len() - n)` removes the front elements (oldest activity) and keeps the last `n` (most recent activity). An attacker flooding Pings has elapsed ≈ 0 seconds and always lands in the protected tail.

**Why round 1 does not protect the attacker:**

Round 1 protects the 8 peers with the lowest `ping_rtt`. The attacker deliberately ignores the server's Ping messages, so `ping_rtt` stays `None` → `u64::MAX`. The round 1 comparator `peer2_ping.cmp(&peer1_ping)` (descending) places the attacker at the front of the sorted list; `truncate` removes the last `n` (best RTT peers), leaving the attacker in the candidate pool. The attacker is then shielded in round 2 via the manipulated timestamp.

**`sort_then_drop` semantics confirmed (`peer_registry.rs` lines 55–63):**

```rust
fn sort_then_drop<T, F>(list: &mut Vec<T>, n: usize, compare: F) {
    list.sort_by(compare);
    if list.len() > n {
        list.truncate(list.len() - n);  // keeps first (len-n), removes last n
    }
}
```

The last `n` elements after a descending sort are the `n` smallest values — i.e., the most recently active peers — which are removed from the eviction candidate pool (protected).

**`Peer` struct confirms the field is attacker-writable (`peer.rs` lines 70–71):**

```rust
/// Ping/Pong message last received time
pub last_ping_protocol_message_received_at: Option<Instant>,
```

No access control or validation guards this field.

## Impact Explanation

An attacker with `k` inbound connections (each flooding Pings) can permanently occupy `k` inbound slots. Once `max_inbound` is reached, every subsequent legitimate peer connection triggers `try_evict_inbound_peer()`, which will always select a legitimate peer for eviction rather than any attacker peer. With `k ≥ EVICTION_PROTECT_PEERS` (8) attacker connections, all "recently active" protection slots are monopolized. This degrades inbound peer diversity, enables targeted eclipse attacks, and can cause CKB network congestion with minimal cost — matching the **High (10001–15000 points)** impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation

The attack requires only a standard TCP connection to the node's P2P port and the ability to send well-formed Ping messages in a loop. No cryptographic material, hashpower, or privileged access is needed. The Ping message format is public. There is no rate limiting, no per-session message budget, and no cost imposed on the sender. The attack is trivially repeatable and can be sustained indefinitely.

## Recommendation

1. **Remove `last_ping_protocol_message_received_at` updates from `ping_received()`** — only a valid Pong with a matching nonce (already validated in `pong_received()`) should refresh this field. Unsolicited one-way Ping messages carry no authenticity and should not influence eviction decisions.
2. **Rate-limit incoming Ping messages** per session (e.g., one per interval window) to prevent flooding.
3. **Rename or separate the field** to make the semantic explicit: "last authenticated round-trip activity" should only be set on verified Pong responses, not on one-way unsolicited messages.

## Proof of Concept

```
1. Connect max_inbound attacker-controlled peers to the target CKB node (standard P2P handshake).
2. Each attacker peer sends a Ping message in a tight loop (every ~1 ms).
3. Each Ping triggers ping_received() → last_ping_protocol_message_received_at = Instant::now().
4. Each attacker peer deliberately ignores server-sent Ping messages (no Pong reply),
   keeping ping_rtt = None → u64::MAX.
5. Attempt to connect a legitimate peer. This triggers try_evict_inbound_peer().
6. Round 1: attacker peers have ping_rtt = u64::MAX → not protected, remain candidates.
7. Round 2: attacker peers have last_ping_protocol_message_received_at elapsed ≈ 0 s
   → protected (kept in tail after descending sort + truncate).
8. Round 3 (connection time) and group selection operate on the remaining legitimate peers.
9. Assert: a legitimate peer is evicted; no attacker peer is ever disconnected.
   Legitimate peer receives PeerError::ReachMaxInboundLimit or is immediately evicted.
```