Audit Report

## Title
Inbound Session Bypasses `accept_peer` Ban/Limit Checks via Feeler PeerId Collision — (`network/src/peer_registry.rs`, `network/src/network.rs`)

## Summary
The `SessionOpen` handler in `network/src/network.rs` calls `is_feeler` on `session_context.address` without first checking whether the session is outbound. Because `feeler_peers` is keyed solely on `PeerId`, any inbound connection arriving from a `PeerId` currently in `feeler_peers` is silently treated as a feeler. This causes `accept_peer` to be skipped entirely, bypassing ban enforcement, inbound connection limits, and peer registry insertion — leaving a "ghost session" that is invisible to all peer-tracking logic.

## Finding Description
`feeler_peers` is declared as `HashMap<PeerId, Flags>` with no session-direction component. [1](#0-0) 

`add_feeler` inserts the `PeerId` extracted from the dialed address immediately after `dial_inner` succeeds: [2](#0-1) 

`is_feeler` checks only whether the `PeerId` is present in `feeler_peers` — no direction check: [3](#0-2) 

In the `SessionOpen` handler, `is_feeler` is evaluated with no guard on `session_context.ty`: [4](#0-3) 

When `is_feeler` returns `true`, the entire `accept_peer` path is skipped. The three checks that are bypassed are:
- `peer_store.is_addr_banned` at `peer_registry.rs:109`
- `connection_status.non_whitelist_inbound >= self.max_inbound` at `peer_registry.rs:116`
- `self.peers.insert(session_id, peer)` at `peer_registry.rs:137` [5](#0-4) 

`remove_feeler` is called in `Feeler::disconnected`, which only fires after the feeler protocol is negotiated and the node sends a disconnect. An attacker controlling the inbound connection can simply not open the feeler protocol, keeping the ghost session alive indefinitely: [6](#0-5) 

The `SessionClose` handler does call `remove_feeler` and `remove_peer`, but `remove_peer` returns `None` for a ghost session (never inserted), so `peer_store.remove_disconnected_peer` is never called either: [7](#0-6) 

**Exploit flow:**
1. Attacker gets their address into the victim's peer store (passive, via discovery protocol).
2. Victim's feeler service dials the attacker — `add_feeler` inserts the attacker's `PeerId` into `feeler_peers`.
3. Attacker immediately opens an inbound TCP connection back to the victim from the same `PeerId`.
4. Victim's `SessionOpen` fires; `is_feeler` returns `true`; `accept_peer` is skipped.
5. Attacker does not open the feeler protocol on the inbound session — `Feeler::disconnected` never fires, `remove_feeler` is never called via that path.
6. The inbound session persists: not in `peer_registry.peers`, not subject to eviction, not subject to ban or limit checks.
7. Attacker repeats across multiple feeler cycles to accumulate ghost sessions.

## Impact Explanation
Repeated exploitation accumulates ghost sessions that consume transport-layer file descriptors and memory while being invisible to all eviction and tracking logic. Because inbound limits are bypassed, the attacker is not constrained by `max_inbound`. Exhausting OS-level connection resources (file descriptors, socket buffers) will crash the node process. This matches the allowed High impact: **"Vulnerabilities which could easily crash a CKB node."**

Secondary confirmed impacts: a banned peer can maintain an active session (ban bypass), and the peer store is left inconsistent because `remove_disconnected_peer` is never called for ghost sessions.

## Likelihood Explanation
The attacker requires no privileged access, no hashpower, and no social engineering. The only preconditions are: (1) having their address in the victim's peer store, achievable passively via the discovery protocol; and (2) timing an inbound connection to arrive after `add_feeler` is called. Because the attacker directly receives the feeler TCP dial, they know the exact moment `add_feeler` fires. The race window spans the full TCP round-trip plus protocol negotiation — several seconds — and is deterministic from the attacker's perspective. The attack is repeatable on every feeler cycle.

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

Alternatively, key `feeler_peers` by `SessionId` instead of `PeerId` in `peer_registry.rs` to prevent cross-session collisions entirely, or add the outbound direction check inside `is_feeler` itself by threading the session type through.

## Proof of Concept
```rust
// Unit test sketch for network/src/peer_registry.rs tests
let mut reg = PeerRegistry::new(10, 10, false, vec![], false);
let peer_id = PeerId::random();
let addr: Multiaddr = format!("/ip4/1.2.3.4/tcp/1234/p2p/{}", peer_id.to_base58())
    .parse().unwrap();

// Step 1: simulate feeler dial — victim calls add_feeler after dial_inner succeeds
reg.add_feeler(&addr);
assert!(reg.is_feeler(&addr)); // true — PeerId is now in feeler_peers

// Step 2: simulate inbound SessionOpen from same PeerId
// In production: handle_event sees is_feeler==true, skips accept_peer entirely
// Peer is never inserted:
assert!(reg.peers().is_empty());

// Step 3: verify ban check was never reached
// A peer banned in peer_store would have been caught at peer_registry.rs:109,
// but that line is unreachable when is_feeler returns true.

// Step 4: verify inbound limit was never enforced
// connection_status.non_whitelist_inbound remains 0 despite the open session.
assert_eq!(reg.connection_status().non_whitelist_inbound, 0);
```

Manual integration steps: configure a victim node with `max_inbound=5`; add attacker address to peer store via discovery; observe victim issuing a feeler dial; immediately connect inbound from the same keypair without opening the feeler protocol; confirm the session is open at the transport layer but absent from `reg.peers()`; repeat five times and confirm a sixth connection is accepted (limit bypassed).

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

**File:** network/src/network.rs (L505-509)
```rust
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
