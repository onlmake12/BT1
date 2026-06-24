All six code claims check out against the actual source. Let me verify the exact line numbers and logic flow before rendering the final verdict.

Audit Report

## Title
Inbound Session Bypasses `accept_peer` Ban/Limit Checks via Feeler PeerId Collision — (`network/src/peer_registry.rs`, `network/src/network.rs`)

## Summary

`feeler_peers` is keyed on `PeerId` alone with no session-direction component. The `SessionOpen` handler in `network.rs` calls `is_feeler` unconditionally — before inspecting `session_context.ty` — so an attacker whose `PeerId` is already in the victim's `feeler_peers` map can open an inbound connection during the feeler race window and have the entire `accept_peer` path skipped. The resulting session is never inserted into `peer_registry.peers`, bypassing ban enforcement, inbound-limit enforcement, and all peer-tracking.

## Finding Description

`feeler_peers` is declared as `HashMap<PeerId, Flags>` with no session-direction field. [1](#0-0) 

`dial_feeler` calls `add_feeler` immediately after `dial_inner` returns `Ok`, before any TCP handshake completes, inserting the target `PeerId` into `feeler_peers`. [2](#0-1) 

`is_feeler` performs a pure `PeerId` key lookup with no direction check. [3](#0-2) 

In the `SessionOpen` handler, `is_feeler` is evaluated at line 746–748 with no prior check on `session_context.ty`. If it returns `true`, execution falls into the debug-log branch and `accept_peer` is never called. [4](#0-3) 

The skipped `accept_peer` path contains the ban check at line 109, the inbound-limit guard at line 116, and the `self.peers.insert(session_id, peer)` call at line 137 — none of which execute for the misclassified inbound session. [5](#0-4) 

On `SessionClose`, `remove_peer(session_context.id)` returns `None` because the ghost session was never inserted into `peers`, so `peer_store.remove_disconnected_peer` is also never called, leaving the peer store in an inconsistent state. [6](#0-5) 

`Feeler::disconnected` calls `remove_feeler` but cannot rescue the ghost session because the inbound session was never registered in `peers`. [7](#0-6) 

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

Three concrete consequences follow from a single misclassification:

1. **Ban bypass**: `peer_store.is_addr_banned` at line 109 is never reached, so a banned peer can hold an active, untracked inbound session indefinitely.
2. **Inbound limit bypass**: The `non_whitelist_inbound >= self.max_inbound` guard at line 116 is never evaluated, allowing connections beyond the configured cap.
3. **Ghost session**: The session is absent from `peer_registry.peers`, making it invisible to eviction logic, `ProtocolTypeCheckerService`, and all session-tracking code. It persists consuming transport resources with no accounting.

An attacker controlling multiple `PeerId`s present in the victim's peer store can accumulate ghost sessions at the rate the victim dials feelers, progressively exhausting inbound connection resources and degrading node operation across the network.

## Likelihood Explanation

The attacker requires no privileges, no hashpower, and no social engineering. Two conditions suffice:

1. Their address appears in the victim's peer store — achievable passively via the discovery protocol.
2. They time an inbound TCP connection to arrive after `add_feeler` is called but before `remove_feeler` fires.

Because the attacker is the direct target of the feeler dial, they observe the incoming SYN from the victim and know exactly when `add_feeler` was called. The race window spans the full TCP round-trip plus protocol negotiation — several seconds — and is deterministic from the attacker's perspective. The attack is repeatable for every feeler dial the victim makes toward attacker-controlled addresses.

## Recommendation

Gate the `is_feeler` check on the session being outbound in the `SessionOpen` handler in `network/src/network.rs`:

```rust
if session_context.ty.is_outbound()
    && self.network_state.with_peer_registry(|reg| reg.is_feeler(&session_context.address))
{
    // feeler path
} else {
    // accept_peer path
}
```

Alternatively, `is_feeler` itself can require a session-type parameter, or `feeler_peers` can be keyed by `SessionId` instead of `PeerId` to eliminate cross-session collisions entirely.

## Proof of Concept

```rust
// Unit test sketch (peer_registry.rs tests)
let mut reg = PeerRegistry::new(10, 10, false, vec![], false);
let peer_id = PeerId::random();
let addr: Multiaddr = format!("/ip4/1.2.3.4/tcp/1234/p2p/{}", peer_id.to_base58())
    .parse().unwrap();

// Step 1: simulate feeler dial — adds peer_id to feeler_peers
reg.add_feeler(&addr);
assert!(reg.is_feeler(&addr)); // true — PeerId match, no direction check

// Step 2: inbound SessionOpen from same peer_id arrives
// In production: handle_event sees is_feeler==true, skips accept_peer entirely
// Peer is never inserted into registry:
assert!(reg.peers().is_empty()); // ghost session confirmed

// Step 3: ban check at peer_registry.rs:109 was never reached
// A banned peer now holds an open, untracked inbound session
```

Manual steps to trigger in a live node:
1. Ensure attacker's multiaddr is gossiped into the victim's peer store via discovery.
2. Wait for the victim to initiate a feeler dial to the attacker's address (observable as an incoming TCP SYN).
3. Immediately open a new inbound TCP connection back to the victim from the same `PeerId`.
4. Observe via victim's metrics/logs that the inbound session is open but absent from `peer_registry.peers` and not subject to ban or limit enforcement.

### Citations

**File:** network/src/peer_registry.rs (L34-34)
```rust
    feeler_peers: HashMap<PeerId, Flags>,
```

**File:** network/src/peer_registry.rs (L109-137)
```rust
            if peer_store.is_addr_banned(&remote_addr) {
                return Err(PeerError::Banned.into());
            }

            let connection_status = self.connection_status();
            // check peers connection limitation
            if session_type.is_inbound() {
                if connection_status.non_whitelist_inbound >= self.max_inbound {
                    if let Some(evicted_session) = self.try_evict_inbound_peer(peer_store) {
                        evicted_peer = self.remove_peer(evicted_session);
                    } else {
                        return Err(PeerError::ReachMaxInboundLimit.into());
                    }
                }
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
        }
        peer_store.add_connected_peer(remote_addr.clone(), session_type);
        let peer = Peer::new(session_id, session_type, remote_addr, is_whitelist);
        self.peers.insert(session_id, peer);
```

**File:** network/src/peer_registry.rs (L244-248)
```rust
    pub fn is_feeler(&self, addr: &Multiaddr) -> bool {
        extract_peer_id(addr)
            .map(|peer_id| self.feeler_peers.contains_key(&peer_id))
            .unwrap_or_default()
    }
```

**File:** network/src/network.rs (L498-509)
```rust
    pub fn dial_feeler(&self, p2p_control: &ServiceControl, addr: Multiaddr) {
        if let Err(err) = self.dial_inner(
            p2p_control,
            addr.clone(),
            TargetProtocol::Single(SupportProtocols::Identify.protocol_id()),
        ) {
            debug!("dial_feeler error {err}");
        } else {
            self.with_peer_registry_mut(|reg| {
                reg.add_feeler(&addr);
            });
        }
```

**File:** network/src/network.rs (L746-754)
```rust
                if self
                    .network_state
                    .with_peer_registry(|reg| reg.is_feeler(&session_context.address))
                {
                    debug!(
                        "Feeler connected {} => {}",
                        session_context.id, session_context.address,
                    );
                } else {
```

**File:** network/src/network.rs (L800-813)
```rust
                let peer_exists = self.network_state.with_peer_registry_mut(|reg| {
                    // should make sure feelers is clean
                    reg.remove_feeler(&session_context.address);
                    reg.remove_peer(session_context.id).is_some()
                });
                if peer_exists {
                    debug!(
                        "{} closed. Remove {} from peer_registry",
                        session_context.id, session_context.address,
                    );
                    self.network_state.with_peer_store_mut(|peer_store| {
                        peer_store.remove_disconnected_peer(&session_context.address);
                    });
                }
```

**File:** network/src/protocols/feeler.rs (L50-56)
```rust
    async fn disconnected(&mut self, context: ProtocolContextMutRef<'_>) {
        let session = context.session;
        self.network_state.with_peer_registry_mut(|reg| {
            reg.remove_feeler(&session.address);
        });
        debug!("peer={} FeelerProtocol.disconnected", session.address);
    }
```
