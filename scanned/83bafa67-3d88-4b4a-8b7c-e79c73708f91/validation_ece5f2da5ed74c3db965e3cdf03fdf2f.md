The code is fully readable. Here is the complete analysis.

---

### Title
Outbound Budget Overcounting: `try_dial_peers` Ignores `block_relay_only_outbound_count`, Allowing Total Outbound to Exceed `max_outbound` — (`network/src/services/outbound_peer.rs`)

---

### Summary

`try_dial_peers` computes how many new full-relay peers to dial using only `non_whitelist_outbound`, never subtracting `block_relay_only_outbound_count`. Because `accept_peer` uses the same incomplete check, all dialed connections are accepted. The result is that the node can hold up to `max_outbound + MAX_OUTBOUND_BLOCK_RELAY` total outbound connections, violating the configured budget.

---

### Finding Description

**`connection_status()` separates the two counters:** [1](#0-0) 

`non_whitelist_outbound` counts only `SessionType::Outbound` peers; `block_relay_only_outbound_count` counts `SessionType::BlockRelayOnly` peers independently.

**`try_dial_peers` subtracts only `non_whitelist_outbound`:** [2](#0-1) 

`block_relay_only_outbound_count` is never subtracted. With `max_outbound=8`, `non_whitelist_outbound=0`, and `block_relay_only_outbound_count=2`, `count = 8 − 0 = 8` instead of the correct `6`.

**`accept_peer` has the same blind spot:** [3](#0-2) 

The guard fires only when `non_whitelist_outbound >= max_outbound`. With `non_whitelist_outbound=0`, all 8 newly dialed full-relay connections pass through, yielding 10 total outbound connections.

---

### Reachable State

The state `non_whitelist_outbound=0, block_relay_only_outbound_count=2` is reachable via the normal lifecycle:

1. Victim fills 8 full-relay outbound slots (`non_whitelist_outbound=8`).
2. Two more outbound connections arrive; `accept_peer` downgrades them to `BlockRelayOnly` because `non_whitelist_outbound >= max_outbound` and `disable_block_relay_only_connection=false`.
3. All 8 full-relay peers disconnect (naturally or attacker-triggered).
4. State: `non_whitelist_outbound=0`, `block_relay_only_outbound_count=2`.
5. `try_dial_peers` fires, computes `count=8`, dials 8 full-relay peers.
6. `accept_peer` accepts all 8 (guard condition `0 >= 8` is false).
7. Total outbound = **10**, exceeding `max_outbound=8`.

The maximum constant `MAX_OUTBOUND_BLOCK_RELAY = 2` bounds the excess. [4](#0-3) 

---

### Impact Explanation

The node exceeds its configured outbound budget by at most `MAX_OUTBOUND_BLOCK_RELAY` (currently 2) connections. This is a real invariant violation but the excess is small and bounded. Resource exhaustion is marginal (2 extra file descriptors / bandwidth slots). Eclipse amplification is negligible — the attacker gains at most 2 extra connection slots beyond the budget, which does not meaningfully shift eclipse probability.

---

### Likelihood Explanation

The trigger state arises naturally without any attacker: block-relay-only connections are established whenever outbound is full, and full-relay peers disconnect routinely. An attacker who controls peers in the victim's peer store can accelerate the trigger by disconnecting simultaneously, but this requires significant prior positioning.

---

### Recommendation

In `try_dial_peers`, subtract both counters:

```rust
let count = status
    .max_outbound
    .saturating_sub(status.non_whitelist_outbound)
    .saturating_sub(status.block_relay_only_outbound_count) as usize;
```

Similarly, the `accept_peer` outbound guard should compare `non_whitelist_outbound + block_relay_only_outbound_count` against `max_outbound` to enforce the combined budget.

---

### Proof of Concept

Construct a `PeerRegistry` with `max_outbound=8`. Register 2 `BlockRelayOnly` peers (simulating the post-disconnect state). Call `connection_status()` and verify `non_whitelist_outbound=0`, `block_relay_only_outbound_count=2`. Then compute `count = max_outbound.saturating_sub(non_whitelist_outbound) = 8`. The correct value is `6`. The node will attempt to dial 8 full-relay peers, and `accept_peer` will accept all 8, yielding 10 total outbound connections against a configured limit of 8.

---

**Severity assessment:** The bug is real, local-testable, and the invariant violation is concrete. However, the excess is bounded by the compile-time constant `MAX_OUTBOUND_BLOCK_RELAY = 2`, making the practical resource and eclipse impact low. This does not meet the "Critical (15001–25000 points)" threshold claimed in the question scope. It is a valid low-to-medium severity logic bug.

### Citations

**File:** network/src/peer_registry.rs (L19-19)
```rust
pub(crate) const MAX_OUTBOUND_BLOCK_RELAY: u32 = 2;
```

**File:** network/src/peer_registry.rs (L123-133)
```rust
            } else if connection_status.non_whitelist_outbound >= self.max_outbound {
                if self.disable_block_relay_only_connection
                    || connection_status.block_relay_only_outbound_count
                        >= self.max_outbound_block_relay
                {
                    return Err(PeerError::ReachMaxOutboundLimit.into());
                } else {
                    peer_store.add_anchors(remote_addr.clone());
                    session_type = SessionType::BlockRelayOnly;
                }
            }
```

**File:** network/src/peer_registry.rs (L299-306)
```rust
        for peer in self.peers.values().filter(|peer| !peer.is_whitelist) {
            if peer.is_outbound() {
                non_whitelist_outbound += 1;
            } else if peer.is_block_relay_only() {
                block_relay_only_outbound_count += 1;
            } else {
                non_whitelist_inbound += 1;
            }
```

**File:** network/src/services/outbound_peer.rs (L100-106)
```rust
        let count = status
            .max_outbound
            .saturating_sub(status.non_whitelist_outbound) as usize;
        if count == 0 {
            self.try_identify_count = 0;
            return;
        }
```
