Based on my analysis of the code, here is my assessment:

---

### Title
Costless Inbound Eviction Loop in `bootnode_mode` Allows Continuous Disruption of Legitimate Peers — (`network/src/network.rs`, `network/src/behaviour.rs`)

### Summary

In `bootnode_mode`, `EventHandler::inbound_eviction` evicts ~50% of all non-whitelist inbound peers on every `SessionOpen` event once the inbound count nears `max_inbound`. Because `Behaviour::score` is unconditionally disabled in production (always returns `0`), an attacker pays zero cost to reconnect after being evicted, enabling a continuous eviction loop that systematically displaces legitimate peers.

### Finding Description

**`inbound_eviction` trigger condition** — fires on every `SessionOpen` when:

```
status.max_inbound <= status.non_whitelist_inbound + 10
```

i.e., when `non_whitelist_inbound >= max_inbound - 10`. [1](#0-0) 

When triggered, it collects **all** non-whitelist inbound peers and evicts every peer at an odd enumeration index (`index & 0x1 != 0`), which is approximately 50% of them. [2](#0-1) 

**Eviction fires before `accept_peer`** — the new attacker session is not yet in the registry when eviction runs, so the count and victim list reflect only existing peers: [3](#0-2) 

**Scoring is permanently disabled in production** — `Behaviour::score` always returns `0` in non-test builds, so no penalty accumulates against the attacker's reconnecting sessions: [4](#0-3) 

**No ban on eviction** — `ban_session` is only called for protocol errors, not for being evicted. `accept_peer` only checks `peer_store.is_addr_banned`, which is never set by the eviction path: [5](#0-4) 

### Impact Explanation

An attacker who opens ≥ `max_inbound - 10` connections to a bootnode keeps the eviction condition permanently satisfied. Every subsequent `SessionOpen` (including the attacker's own reconnections) evicts ~50% of all non-whitelist inbound peers. Legitimate peers that are evicted must re-discover and re-connect to the bootnode; the attacker reconnects immediately at zero cost. Over repeated cycles, the attacker's sessions dominate the bootnode's inbound slots, preventing legitimate peers from using the bootnode for peer discovery and network bootstrapping.

### Likelihood Explanation

- `bootnode_mode` is a supported, documented production configuration for CKB bootnodes.
- Opening TCP connections to a public P2P node is an unprivileged, zero-cost operation.
- No rate limiting, no reconnection penalty, and no ban mechanism exist on the eviction path.
- The attack is self-sustaining: the attacker's own connections are also evicted at odd indices, but they reconnect faster than honest peers, so the honest-to-attacker ratio degrades monotonically.

### Recommendation

1. **Re-enable scoring or add a reconnection cost**: `Behaviour::score` should apply a negative score to peers that are evicted and reconnect rapidly, eventually banning them.
2. **Rate-limit inbound connections per IP/subnet**: Reject or throttle connections from addresses that have reconnected more than N times within a window.
3. **Fix the eviction selection**: Instead of evicting by HashMap iteration order (non-deterministic, unrelated to peer quality), use the same protection heuristics as `try_evict_inbound_peer` (ping RTT, last message time, connection age).
4. **Decouple eviction from `SessionOpen`**: Eviction should not fire on every new connection; it should be rate-limited or triggered only when the registry is actually full.

### Proof of Concept

```
Setup: bootnode_mode=true, max_inbound=20
1. Attacker opens 10 connections → non_whitelist_inbound=10 (threshold: 20-10=10, condition met)
2. Attacker opens connection #11 → SessionOpen fires → inbound_eviction() runs
   → ~5 of the 10 existing peers (odd indices) are disconnected
3. Attacker's evicted connections immediately reconnect → SessionOpen fires again
   → another ~50% eviction round
4. Honest peers evicted in step 2 attempt to reconnect but are slower
5. After 100 cycles: attacker sessions fill the majority of inbound slots
```

The root cause is the combination of:
- `Behaviour::score` returning `0` unconditionally in production [4](#0-3) 
- `inbound_eviction` having no reconnection cost or rate limit [6](#0-5) 
- Eviction firing before the new session is registered, making the attacker's new connection a free trigger [7](#0-6)

### Citations

**File:** network/src/network.rs (L580-604)
```rust
    fn inbound_eviction(&self) -> Vec<PeerIndex> {
        if self.network_state.config.bootnode_mode {
            let status = self.network_state.connection_status();

            if status.max_inbound <= status.non_whitelist_inbound.saturating_add(10) {
                self.network_state
                    .with_peer_registry(|registry| {
                        registry
                            .peers()
                            .values()
                            .filter(|peer| peer.is_inbound() && !peer.is_whitelist)
                            .map(|peer| peer.session_id)
                            .collect::<Vec<SessionId>>()
                    })
                    .into_iter()
                    .enumerate()
                    .filter_map(|(index, peer)| if index & 0x1 != 0 { Some(peer) } else { None })
                    .collect()
            } else {
                Vec::new()
            }
        } else {
            Vec::new()
        }
    }
```

**File:** network/src/network.rs (L734-755)
```rust
                let iter = self.inbound_eviction();

                let control = context.control().clone().into();

                for peer in iter {
                    if let Err(err) =
                        disconnect_with_message(&control, peer, "bootnode random eviction")
                    {
                        debug!("Inbound eviction failed {:?}, error: {:?}", peer, err);
                    }
                }

                if self
                    .network_state
                    .with_peer_registry(|reg| reg.is_feeler(&session_context.address))
                {
                    debug!(
                        "Feeler connected {} => {}",
                        session_context.id, session_context.address,
                    );
                } else {
                    match self.network_state.accept_peer(&session_context) {
```

**File:** network/src/behaviour.rs (L26-27)
```rust
        #[cfg(not(test))]
        0
```

**File:** network/src/peer_registry.rs (L109-111)
```rust
            if peer_store.is_addr_banned(&remote_addr) {
                return Err(PeerError::Banned.into());
            }
```
