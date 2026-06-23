The code is present and I have everything needed to make a determination. Let me analyze the exact logic.

**Key facts from the code:**

**`ping_rtt` is set** in `ping.rs` line 75: [1](#0-0) 
`peer.ping_rtt = Some(now.saturating_duration_since(last_ping))` — full sub-millisecond precision stored.

**Eviction comparison** in `peer_registry.rs` lines 155–163: [2](#0-1) 
Uses `.as_secs()` — truncates to whole seconds. Any RTT < 1s maps to `0`.

**`sort_then_drop` semantics** (lines 55–63): [3](#0-2) 
Sorts descending by ping, then `truncate(list.len() - n)` keeps the first `list.len()-n` elements (highest ping = eviction candidates), dropping the last `n` (lowest ping = protected). With all sub-second peers mapping to `0`, the sort order among them is arbitrary, so which `n` get protected is effectively random.

**`EVICTION_PROTECT_PEERS = 8`**: [4](#0-3) 

**The second protection round** (lines 168–183) has the same `.as_secs()` truncation on `last_ping_protocol_message_received_at`: [5](#0-4) 

---

### Title
RTT Eviction Protection Defeated by `as_secs()` Truncation — (`network/src/peer_registry.rs`)

### Summary
`try_evict_inbound_peer` compares `ping_rtt` values using `.as_secs()`, truncating all sub-second RTTs to `0`. This makes every peer with RTT < 1 s indistinguishable, collapsing the "protect lowest-ping peers" invariant into a random selection among all sub-second peers. An attacker who responds to pings immediately (0 ms RTT) is treated identically to a legitimate peer with 900 ms RTT, allowing the attacker's connections to be randomly protected while legitimate peers are evicted.

### Finding Description
In `peer_registry.rs`, `try_evict_inbound_peer` runs three sequential protection passes before randomly evicting a peer. The first pass is intended to protect the 8 peers with the lowest measured RTT:

```rust
let peer1_ping = peer1.ping_rtt.map(|p| p.as_secs()).unwrap_or(u64::MAX);
let peer2_ping = peer2.ping_rtt.map(|p| p.as_secs()).unwrap_or(u64::MAX);
peer2_ping.cmp(&peer1_ping)
```

`Duration::as_secs()` discards the sub-second component. In a real network, virtually all peers have RTTs between 10 ms and 900 ms, so every peer maps to `0`. The sort produces no meaningful ordering, and `sort_then_drop` retains an arbitrary 8 peers as "protected." The second pass (`last_ping_protocol_message_received_at`) has the same `.as_secs()` truncation, compounding the issue.

An attacker who controls multiple inbound connections and replies to pings with 0 ms RTT is placed in the same equivalence class as all legitimate peers. The attacker's connections have an equal chance of landing in the protected set, while legitimate peers have an equal chance of becoming eviction candidates.

### Impact Explanation
The eviction protection is a key defense against inbound-slot exhaustion and eclipse attacks. By defeating the lowest-ping protection, an attacker can:
1. Maintain a disproportionate share of inbound slots over time.
2. Gradually displace legitimate peers through repeated connection cycling.
3. Increase the probability of a partial or full eclipse of the victim node.

The impact is bounded (Low, 501–2000) because: the attacker cannot deterministically target a specific legitimate peer; two additional protection layers (connection time, network group) remain partially effective; and a full eclipse requires controlling all outbound connections as well.

### Likelihood Explanation
The preconditions are achievable by any unprivileged attacker: open multiple TCP connections to the victim, complete the handshake, and reply to every ping message immediately. No special privileges, hashpower, or key material are required. The nonce check in `pong_received` only validates that the nonce matches the last sent ping — it does not prevent fast replies.

### Recommendation
Replace `.as_secs()` with `.as_millis()` (or `.as_micros()`) in both comparison closures inside `try_evict_inbound_peer`. This preserves sub-second ordering and restores the intended invariant:

```rust
// Before
peer1.ping_rtt.map(|p| p.as_secs()).unwrap_or(u64::MAX)
// After
peer1.ping_rtt.map(|p| p.as_millis() as u64).unwrap_or(u64::MAX)
```

Apply the same fix to the `last_ping_protocol_message_received_at` comparison.

### Proof of Concept
```rust
// Construct 20 inbound peers: 10 legitimate (RTT 10–900 ms), 10 attacker (RTT 0 ms).
// Call try_evict_inbound_peer repeatedly until all legitimate peers are evicted.
// With as_secs(), all peers have ping_key=0, so eviction is random.
// With as_millis(), legitimate peers (10–900 ms) are always protected over attacker peers (0 ms).
let mut rng_evictions = 0;
for _ in 0..100 {
    let victim = registry.try_evict_inbound_peer(&peer_store).unwrap();
    if legitimate_sessions.contains(&victim) { rng_evictions += 1; }
}
// Expect rng_evictions ≈ 50 (random) with as_secs(), ≈ 0 with as_millis().
assert!(rng_evictions > 30, "legitimate peers evicted at near-random rate");
```

### Citations

**File:** network/src/protocols/ping.rs (L75-75)
```rust
                peer.ping_rtt = Some(now.saturating_duration_since(last_ping));
```

**File:** network/src/peer_registry.rs (L17-17)
```rust
pub(crate) const EVICTION_PROTECT_PEERS: usize = 8;
```

**File:** network/src/peer_registry.rs (L55-63)
```rust
fn sort_then_drop<T, F>(list: &mut Vec<T>, n: usize, compare: F)
where
    F: FnMut(&T, &T) -> std::cmp::Ordering,
{
    list.sort_by(compare);
    if list.len() > n {
        list.truncate(list.len() - n);
    }
}
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

**File:** network/src/peer_registry.rs (L173-181)
```rust
                let peer1_last_message = peer1
                    .last_ping_protocol_message_received_at
                    .map(|t| now.saturating_duration_since(t).as_secs())
                    .unwrap_or_else(|| u64::MAX);
                let peer2_last_message = peer2
                    .last_ping_protocol_message_received_at
                    .map(|t| now.saturating_duration_since(t).as_secs())
                    .unwrap_or_else(|| u64::MAX);
                peer2_last_message.cmp(&peer1_last_message)
```
