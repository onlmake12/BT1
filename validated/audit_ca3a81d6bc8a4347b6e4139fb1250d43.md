### Title
Ordered Protocol-Type Check Allows Feeler+Partial-Protocol Peers to Evade Eviction — (`network/src/services/protocol_type_checker.rs`)

---

### Summary

`ProtocolTypeCheckerService::opened_protocol_type` checks for the Feeler protocol **before** checking for the full required-protocol set. Because the first matching branch wins, any peer that opens the Feeler protocol alongside an incomplete set of required protocols is classified as the valid `Feeler` type instead of the error `Incomplete` type. This is the direct CKB analog of the ERC721/ERC1155 priority-ordering bug: an entity that satisfies both type conditions is routed into the wrong branch, bypassing the intended enforcement.

---

### Finding Description

The file-level comment in `protocol_type_checker.rs` defines exactly two valid peer states:

> 1. fully-opened: all sub-protocols (except feeler) are opened.
> 2. feeler: **only** open feeler protocol is open.

The implementation of `opened_protocol_type` is:

```rust
fn opened_protocol_type(&self, peer: &Peer) -> Result<ProtocolType, ProtocolTypeError> {
    if peer
        .protocols
        .contains_key(&SupportProtocols::Feeler.protocol_id())
    {
        Ok(ProtocolType::Feeler)          // ← first branch wins unconditionally
    } else if self
        .fully_open_required_protocol_ids
        .iter()
        .all(|p_id| peer.protocols.contains_key(p_id))
    {
        Ok(ProtocolType::FullyOpen)
    } else {
        Err(ProtocolTypeError::Incomplete) // ← eviction trigger
    }
}
```

The first branch fires whenever the Feeler protocol ID is present in `peer.protocols`, **regardless of what other protocols are also open**. The "only" constraint stated in the comment is never enforced. A peer that opens Feeler + a partial set of required protocols (e.g., Feeler + Sync but not Relay/Ping/Discovery) satisfies the first branch and is returned as `Ok(ProtocolType::Feeler)`. The checker's caller (`check_protocol_type`) only disconnects on `Err`, so this peer is never evicted by the checker. [1](#0-0) [2](#0-1) [3](#0-2) 

The Feeler protocol handler (`feeler.rs`) does attempt to disconnect any peer that opens Feeler:

```rust
async fn connected(&mut self, context: ProtocolContextMutRef<'_>, _version: &str) {
    ...
    if let Err(err) =
        async_disconnect_with_message(context.control(), session.id, "feeler connection").await
    {
        debug!("Disconnect failed {:?}, error: {:?}", session.id, err);  // ← only logged
    }
}
``` [4](#0-3) 

The disconnect failure is **only logged** — it is not retried and does not trigger any fallback. If `async_disconnect_with_message` fails (e.g., due to a transient send-queue error), the peer remains in the registry with Feeler + partial protocols in its `peer.protocols` map. Every subsequent 30-second tick of `ProtocolTypeCheckerService` will re-classify it as `Feeler` (valid) and leave it connected indefinitely. [5](#0-4) 

---

### Impact Explanation

The `ProtocolTypeCheckerService` exists specifically to close connections from peers that avoid opening the Sync protocol in order to evade the sync-layer eviction mechanism. A malicious inbound peer that opens Feeler + Sync (but omits Relay, Ping, or Discovery) would:

1. Be classified as `Feeler` by the checker → **not disconnected by the checker**.
2. Trigger the Feeler handler → disconnect attempted.
3. If the disconnect fails → peer remains connected, occupying an inbound slot, with the checker permanently misclassifying it as a valid Feeler peer on every 30-second interval.

The peer holds an inbound connection slot without participating in the full relay/discovery protocol, degrading the node's effective peer capacity and undermining the eviction defense the checker was designed to provide. [6](#0-5) 

---

### Likelihood Explanation

An unprivileged inbound peer controls exactly which protocol IDs it opens. Opening Feeler alongside a partial required-protocol set requires no special privilege, no key material, and no majority hashpower — it is a straightforward P2P-layer action. The Feeler-handler disconnect failure is an edge case, but the window is real: the disconnect is asynchronous, the error path is silent (debug-log only), and the checker re-runs every 30 seconds without re-attempting the disconnect.

---

### Recommendation

Enforce the "only" constraint stated in the comment. Before returning `Ok(Feeler)`, verify that no required protocol is simultaneously open:

```rust
fn opened_protocol_type(&self, peer: &Peer) -> Result<ProtocolType, ProtocolTypeError> {
    let has_feeler = peer
        .protocols
        .contains_key(&SupportProtocols::Feeler.protocol_id());
    let has_all_required = self
        .fully_open_required_protocol_ids
        .iter()
        .all(|p_id| peer.protocols.contains_key(p_id));

    if has_all_required && !has_feeler {
        Ok(ProtocolType::FullyOpen)
    } else if has_feeler && !has_all_required {
        Ok(ProtocolType::Feeler)
    } else {
        Err(ProtocolTypeError::Incomplete)
    }
}
```

This mirrors the recommended fix in the original report: check the more-specific / higher-priority condition first and treat the ambiguous overlap as an error rather than silently routing it into the wrong branch.

---

### Proof of Concept

1. A malicious peer dials the CKB node and completes the TCP/P2P handshake (inbound session).
2. The peer opens the Feeler protocol ID **and** the Sync protocol ID, but deliberately omits Relay, Ping, and Discovery.
3. The Feeler handler fires and calls `async_disconnect_with_message`. Suppose the send queue is momentarily full; the call returns an error, which is only `debug!`-logged. The peer remains in `PeerRegistry::peers`.
4. Thirty seconds later `ProtocolTypeCheckerService::check_protocol_type` iterates all peers. For this peer, `opened_protocol_type` checks `peer.protocols.contains_key(Feeler)` → `true` → returns `Ok(Feeler)`. The checker does **not** disconnect.
5. This repeats every 30 seconds. The peer holds an inbound slot indefinitely, with Sync open but Relay/Ping/Discovery absent, evading the eviction mechanism the checker was designed to enforce. [2](#0-1) [7](#0-6)

### Citations

**File:** network/src/services/protocol_type_checker.rs (L1-8)
```rust
/// CKB evicts inactive peers in `sync` protocol; but due to P2P connection design,
/// a malicious peer may choose not to open `sync` protocol, to sneak from the eviction mechanism;
/// this service periodically check peers opened sub-protocols, to make sure no malicious connection.
///
/// Currently, 2 sub-protocols types are valid:
///
/// 1. fully-opened: all sub-protocols(except feeler) are opened.
/// 2. feeler: only open feeler protocol is open.
```

**File:** network/src/services/protocol_type_checker.rs (L23-24)
```rust
const TIMEOUT: Duration = Duration::from_secs(10);
const CHECK_INTERVAL: Duration = Duration::from_secs(30);
```

**File:** network/src/services/protocol_type_checker.rs (L81-106)
```rust
    pub(crate) fn check_protocol_type(&self) {
        self.network_state.with_peer_registry(|reg| {
            let now = Instant::now();
            for (session_id, peer) in reg.peers() {
                // skip just connected peers
                if now.saturating_duration_since(peer.connected_time) < TIMEOUT {
                    continue;
                }

                // check open protocol type
                if let Err(err) = self.opened_protocol_type(peer) {
                    debug!(
                        "Close peer {:?} due to open protocols error: {}",
                        peer.connected_addr, err
                    );
                    if let Err(err) = disconnect_with_message(
                        &self.p2p_control,
                        *session_id,
                        &format!("open protocols error: {err}"),
                    ) {
                        debug!("Disconnect failed {session_id:?}, error: {err:?}");
                    }
                }
            }
        });
    }
```

**File:** network/src/services/protocol_type_checker.rs (L108-123)
```rust
    fn opened_protocol_type(&self, peer: &Peer) -> Result<ProtocolType, ProtocolTypeError> {
        if peer
            .protocols
            .contains_key(&SupportProtocols::Feeler.protocol_id())
        {
            Ok(ProtocolType::Feeler)
        } else if self
            .fully_open_required_protocol_ids
            .iter()
            .all(|p_id| peer.protocols.contains_key(p_id))
        {
            Ok(ProtocolType::FullyOpen)
        } else {
            Err(ProtocolTypeError::Incomplete)
        }
    }
```

**File:** network/src/protocols/feeler.rs (L27-48)
```rust
    async fn connected(&mut self, context: ProtocolContextMutRef<'_>, _version: &str) {
        let session = context.session;
        if context.session.ty.is_outbound() {
            let flags = self.network_state.with_peer_registry(|reg| {
                if let Some(p) = reg.feeler_flags(&session.address) {
                    p
                } else {
                    Flags::COMPATIBILITY
                }
            });
            self.network_state.with_peer_store_mut(|peer_store| {
                peer_store.add_outbound_addr(session.address.clone(), flags);
            });
        }

        debug!("peer={} FeelerProtocol.connected", session.address);
        if let Err(err) =
            async_disconnect_with_message(context.control(), session.id, "feeler connection").await
        {
            debug!("Disconnect failed {:?}, error: {:?}", session.id, err);
        }
    }
```

**File:** network/src/peer_registry.rs (L22-35)
```rust
pub struct PeerRegistry {
    peers: HashMap<SessionId, Peer>,
    // max inbound limitation
    max_inbound: u32,
    // max outbound limitation
    max_outbound: u32,
    // max block-relay only outbound limitation
    // We do not relay tx or addr messages with these peers
    max_outbound_block_relay: u32,
    // Only whitelist peers or allow all peers.
    whitelist_only: bool,
    whitelist_peers: HashSet<PeerId>,
    feeler_peers: HashMap<PeerId, Flags>,
    disable_block_relay_only_connection: bool,
```
