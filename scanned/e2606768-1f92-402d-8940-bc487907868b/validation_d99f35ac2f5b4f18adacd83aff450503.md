Looking at the exact code path in `network/src/network.rs` and `network/src/peer_registry.rs`:

**`handle_event` → `SessionOpen` branch** (lines 746–753 of `network.rs`): [1](#0-0) 

```rust
if self
    .network_state
    .with_peer_registry(|reg| reg.is_feeler(&session_context.address))
{
    debug!("Feeler connected ...");
    // accept_peer is NOT called
} else {
    match self.network_state.accept_peer(&session_context) { ... }
}
```

**`is_feeler`** (peer_registry.rs lines 244–248) checks only whether the peer ID is in `feeler_peers` — it does **not** check `session_context.ty` (inbound vs. outbound): [2](#0-1) 

**`dial_feeler`** (network.rs lines 498–510) adds the peer ID to `feeler_peers` unconditionally after issuing the outbound dial: [3](#0-2) 

**`accept_peer`** (peer_registry.rs lines 86–139) is where ban checks, inbound limit enforcement, and `peer_store.add_connected_peer` all live — and it inserts into `self.peers`: [4](#0-3) 

**`connection_status`** counts only peers in `self.peers` (lines 294–316): [5](#0-4) 

---

### Title
Inbound session from feeler-listed peer bypasses `accept_peer`, ban checks, and inbound limit — (`network/src/network.rs`, `network/src/peer_registry.rs`)

### Summary

When the victim node dials a feeler to peer X, `add_feeler` inserts X's peer ID into `feeler_peers`. If X then opens an **inbound** TCP session before the feeler dial completes or times out, `handle_event`'s `SessionOpen` branch calls `is_feeler`, which returns `true` (peer ID match only, no direction check). The `else` branch — containing `accept_peer` — is never reached. The session is live at the transport layer but absent from `PeerRegistry.peers` and uncounted by `connection_status()`.

### Finding Description

`is_feeler` (peer_registry.rs:244–248) checks only `feeler_peers.contains_key(&peer_id)`. It does not inspect `session_context.ty` to confirm the session is outbound. `dial_feeler` (network.rs:498–510) adds the peer ID to `feeler_peers` as soon as the outbound dial is issued, before any response. The window between `add_feeler` and `remove_feeler` (which only fires on `SessionClose` or `dial_failed`) is the race window. An attacker who knows (or can observe) that the victim has dialed them as a feeler can open an inbound connection during that window. The `SessionOpen` handler takes the feeler branch, logs "Feeler connected", and returns without calling `accept_peer`. Consequently:

- `peer_store.is_addr_banned` is never checked.
- `connection_status.non_whitelist_inbound >= max_inbound` is never checked.
- `peer_store.add_connected_peer` is never called.
- `self.peers.insert(session_id, peer)` is never called.

The session remains open at the tentacle p2p layer. Protocol handlers (sync, relay, discovery) receive messages from this session via their own `session_id`-keyed dispatch, independent of `PeerRegistry.peers`.

### Impact Explanation

1. **Ban bypass**: A banned peer whose address is in `feeler_peers` can maintain an active inbound session. `peer_store.is_addr_banned` is only called inside `accept_peer`.
2. **Inbound limit bypass**: `connection_status().non_whitelist_inbound` is derived from `self.peers` (peer_registry.rs:299–305). The hidden session is not counted, so the limit is never enforced for it. An attacker can hold multiple such sessions simultaneously (one per feeler dial window), exhausting real inbound slots for honest peers — a prerequisite for eclipse attacks.
3. **Invisible session**: `connected_peers()`, `peers()`, and `connection_status()` all operate on `self.peers` and will not reflect this session, defeating any monitoring or eviction logic.

### Likelihood Explanation

The precondition — victim has dialed attacker as a feeler — is reachable without any privileged access. The feeler service (`OutboundPeerService` / `Feeler` protocol) periodically selects addresses from the peer store and calls `dial_feeler`. An attacker who has previously been gossiped into the victim's peer store (via discovery) will eventually be dialed as a feeler. The race window is up to `DIAL_HANG_TIMEOUT` (300 seconds, network.rs:71), giving the attacker ample time to open the inbound connection. No hashpower, key material, or operator access is required.

### Recommendation

In `handle_event`'s `SessionOpen` branch, gate the feeler fast-path on the session being **outbound**:

```rust
if session_context.ty == RawSessionType::Outbound
    && self.network_state.with_peer_registry(|reg| reg.is_feeler(&session_context.address))
{
    // feeler outbound path
} else {
    // normal accept_peer path
}
```

Alternatively, `is_feeler` itself can be changed to also require that the session type is outbound, or `feeler_peers` entries can be keyed on `(PeerId, SessionId)` so that only the specific outbound session is matched.

### Proof of Concept

State-level test (no network required):

```rust
let mut registry = PeerRegistry::new(2, 8, false, vec![], false);
let attacker_addr: Multiaddr = "/ip4/1.2.3.4/tcp/8115/p2p/QmAttacker".parse().unwrap();

// Victim dials attacker as feeler
registry.add_feeler(&attacker_addr);
assert!(registry.is_feeler(&attacker_addr));

// Attacker opens inbound session — simulate what handle_event does
// (is_feeler returns true, so accept_peer is skipped)
let session_id = SessionId::new(42);
// accept_peer is NOT called here — this is the bug

// Assert: session absent from registry
assert!(registry.get_peer(session_id).is_none());

// Assert: connection_status does not count it
let status = registry.connection_status();
assert_eq!(status.non_whitelist_inbound, 0); // hidden session not counted
assert_eq!(status.total, 0);
```

The session at the transport layer is live; the registry is unaware of it. A banned attacker or one exceeding inbound limits can exploit this window.

### Citations

**File:** network/src/network.rs (L498-510)
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
    }
```

**File:** network/src/network.rs (L746-793)
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
                    match self.network_state.accept_peer(&session_context) {
                        Ok(Some(evicted_peer)) => {
                            debug!(
                                "Disconnect peer, {} => {}",
                                evicted_peer.session_id, evicted_peer.connected_addr,
                            );
                            if let Err(err) = disconnect_with_message(
                                &control,
                                evicted_peer.session_id,
                                "evict because accepted better peer",
                            ) {
                                debug!(
                                    "Disconnect failed {:?}, error: {:?}",
                                    evicted_peer.session_id, err
                                );
                            }
                        }
                        Ok(None) => debug!(
                            "{} open, registry {} success",
                            session_context.id, session_context.address,
                        ),
                        Err(err) => {
                            debug!(
                                "Peer registry failed {:?}. Disconnect {} => {}",
                                err, session_context.id, session_context.address,
                            );
                            if let Err(err) = disconnect_with_message(
                                &control,
                                session_context.id,
                                "reject peer connection",
                            ) {
                                debug!(
                                    "Disconnect failed {:?}, error: {:?}",
                                    session_context.id, err
                                );
                            }
                        }
                    }
                }
```

**File:** network/src/peer_registry.rs (L86-139)
```rust
    pub(crate) fn accept_peer(
        &mut self,
        remote_addr: Multiaddr,
        session_id: SessionId,
        raw_session_type: RawSessionType,
        peer_store: &mut PeerStore,
    ) -> Result<Option<Peer>, Error> {
        if self.peers.contains_key(&session_id) {
            return Err(PeerError::SessionExists(session_id).into());
        }
        let peer_id = extract_peer_id(&remote_addr).expect("opened session should have peer id");
        if self.get_key_by_peer_id(&peer_id).is_some() {
            return Err(PeerError::PeerIdExists(peer_id).into());
        }

        let is_whitelist = self.whitelist_peers.contains(&peer_id);
        let mut evicted_peer: Option<Peer> = None;

        let mut session_type: SessionType = raw_session_type.into();
        if !is_whitelist {
            if self.whitelist_only {
                return Err(PeerError::NonReserved.into());
            }
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
        Ok(evicted_peer)
    }
```

**File:** network/src/peer_registry.rs (L244-248)
```rust
    pub fn is_feeler(&self, addr: &Multiaddr) -> bool {
        extract_peer_id(addr)
            .map(|peer_id| self.feeler_peers.contains_key(&peer_id))
            .unwrap_or_default()
    }
```

**File:** network/src/peer_registry.rs (L294-316)
```rust
    pub(crate) fn connection_status(&self) -> ConnectionStatus {
        let total = self.peers.len() as u32;
        let mut non_whitelist_inbound: u32 = 0;
        let mut non_whitelist_outbound: u32 = 0;
        let mut block_relay_only_outbound_count: u32 = 0;
        for peer in self.peers.values().filter(|peer| !peer.is_whitelist) {
            if peer.is_outbound() {
                non_whitelist_outbound += 1;
            } else if peer.is_block_relay_only() {
                block_relay_only_outbound_count += 1;
            } else {
                non_whitelist_inbound += 1;
            }
        }
        ConnectionStatus {
            total,
            non_whitelist_inbound,
            non_whitelist_outbound,
            block_relay_only_outbound_count,
            max_inbound: self.max_inbound,
            max_outbound: self.max_outbound,
        }
    }
```
