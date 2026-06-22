### Title
Any Connected Peer Can Subscribe to Light Client State Broadcasts Without Peer-Type Verification — (`util/light-client-protocol-server/src/components/get_last_state.rs`)

---

### Summary

`GetLastStateProcess::execute()` sets `peer.if_lightclient_subscribed = true` on any peer that sends a `GetLastState` message with `subscribe=true`, without verifying that the peer is a legitimate light-client-only peer. Because `ProtocolTypeCheckerService::opened_protocol_type()` only requires that a peer has opened *at least* the required full-node protocols, a full-node peer that additionally opens the `LightClient` protocol passes all checks and can subscribe. On every new block, the node then performs MMR root computation and sends `SendLastState` to every subscribed peer — including unintended full-node peers — wasting CPU and bandwidth proportional to the number of malicious subscribers.

---

### Finding Description

The `LightClientProtocol` handler in `util/light-client-protocol-server/src/lib.rs` dispatches incoming messages to sub-processors. When a peer sends `GetLastState` with `subscribe = true`, `GetLastStateProcess::execute()` unconditionally marks the peer as a light-client subscriber:

```rust
// util/light-client-protocol-server/src/components/get_last_state.rs
pub(crate) async fn execute(self) -> Status {
    let subscribe: bool = self.message.subscribe().into();
    if subscribe {
        self.nc.with_peer_mut(
            self.peer,
            Box::new(|peer| {
                peer.if_lightclient_subscribed = true;  // ← no type check
            }),
        );
    }
    ...
}
``` [1](#0-0) 

The `if_lightclient_subscribed` flag is defined on the shared `Peer` struct, which is used for **all** peer types: [2](#0-1) 

The peer-type enforcement service, `ProtocolTypeCheckerService::opened_protocol_type()`, only checks that a peer has opened **all** required full-node protocols (sync, relay, etc.). It does not prevent a peer from additionally opening the `LightClient` protocol:

```rust
fn opened_protocol_type(&self, peer: &Peer) -> Result<ProtocolType, ProtocolTypeError> {
    if peer.protocols.contains_key(&SupportProtocols::Feeler.protocol_id()) {
        Ok(ProtocolType::Feeler)
    } else if self.fully_open_required_protocol_ids
        .iter()
        .all(|p_id| peer.protocols.contains_key(p_id))  // ← AT LEAST, not EXACTLY
    {
        Ok(ProtocolType::FullyOpen)
    } else {
        Err(ProtocolTypeError::Incomplete)
    }
}
``` [3](#0-2) 

A full-node peer that opens all required protocols **plus** `LightClient` is classified as `FullyOpen` and never disconnected. It can then send `GetLastState(subscribe=true)` and become a subscriber.

On every new block, both `submit_block` (RPC) and `build_and_broadcast_compact_block` (relay) iterate over all peers with `if_lightclient_subscribed = true`, compute the MMR chain root, build a `VerifiableHeader`, and broadcast `SendLastState` to each: [4](#0-3) [5](#0-4) 

---

### Impact Explanation

Every subscribed peer forces the node to perform an MMR root lookup and message construction per block. An attacker controlling `N` peers (bounded by `max_inbound` + `max_outbound`) can cause the node to perform `N` extra MMR root computations and `N` extra network sends on every block arrival. This amplifies CPU and bandwidth consumption proportional to the number of malicious subscribers, degrading service for legitimate light clients and the node itself. The `SendLastState` message is also sent over the `LightClient` protocol channel, so the peer must have opened that protocol — but nothing prevents a full-node peer from doing so.

---

### Likelihood Explanation

Any unprivileged peer reachable over the P2P network can execute this attack. No special keys, privileges, or majority hashpower are required. The attacker only needs to:
1. Connect to the target node (standard P2P connection).
2. Open the required full-node protocols to pass `ProtocolTypeCheckerService`.
3. Additionally open the `LightClient` protocol.
4. Send one `GetLastState` message with `subscribe=true`.

This is a straightforward, low-cost action available to any external peer.

---

### Recommendation

In `GetLastStateProcess::execute()`, before setting `if_lightclient_subscribed = true`, verify that the peer has opened **only** the `LightClient` protocol (i.e., has not opened the full-node sync/relay protocols). Concretely:

```rust
if subscribe {
    self.nc.with_peer_mut(self.peer, Box::new(|peer| {
        // Only allow pure light-client peers to subscribe
        let has_fullnode_protocols = /* check peer.protocols for sync/relay IDs */;
        if !has_fullnode_protocols {
            peer.if_lightclient_subscribed = true;
        }
    }));
}
```

Alternatively, `ProtocolTypeCheckerService` should be extended to recognize a third valid type — `LightClientOnly` — and reject peers that open both full-node and light-client protocols simultaneously.

---

### Proof of Concept

1. Attacker connects to a CKB full node.
2. Attacker opens `Sync`, `RelayV3`, `Identify`, `Ping`, and `LightClient` protocols — satisfying `ProtocolTypeCheckerService`'s `FullyOpen` check.
3. Attacker sends a `LightClientMessage::GetLastState` with `subscribe = true` on the `LightClient` protocol channel.
4. `GetLastStateProcess::execute()` sets `peer.if_lightclient_subscribed = true` with no type check.
5. On every subsequent block (via `submit_block` RPC or `build_and_broadcast_compact_block`), the node computes `snapshot.chain_root_mmr(header.number() - 1).get_root()`, builds a `VerifiableHeader`, and sends `SendLastState` to the attacker's peer.
6. Repeating with `max_inbound` connections maximizes resource drain per block. [1](#0-0) [3](#0-2) [6](#0-5) [7](#0-6)

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state.rs (L29-38)
```rust
    pub(crate) async fn execute(self) -> Status {
        let subscribe: bool = self.message.subscribe().into();
        if subscribe {
            self.nc.with_peer_mut(
                self.peer,
                Box::new(|peer| {
                    peer.if_lightclient_subscribed = true;
                }),
            );
        }
```

**File:** network/src/peer.rs (L86-88)
```rust
    /// Whether the remote peer is a light client, and it subscribes the chain state.
    pub if_lightclient_subscribed: bool,
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

**File:** rpc/src/module/miner.rs (L323-367)
```rust
            let parent_chain_root = {
                let mmr = snapshot.chain_root_mmr(header.number() - 1);
                match mmr.get_root() {
                    Ok(root) => root,
                    Err(err) => {
                        error!("Generate last state to light client failed: {:?}", err);
                        return Ok(header.hash().into());
                    }
                }
            };

            let tip_header = packed::VerifiableHeader::new_builder()
                .header(header.data())
                .uncles_hash(block.calc_uncles_hash())
                .extension(Pack::pack(&block.extension()))
                .parent_chain_root(parent_chain_root)
                .build();
            let light_client_message = {
                let content = packed::SendLastState::new_builder()
                    .last_header(tip_header)
                    .build();
                packed::LightClientMessage::new_builder()
                    .set(content)
                    .build()
            };
            let light_client_peers: HashSet<PeerIndex> = self
                .network_controller
                .connected_peers()
                .into_iter()
                .filter(|(_id, peer)| peer.if_lightclient_subscribed)
                .map(|(id, _)| id)
                .collect();
            let async_control = self.network_controller.async_p2p_control();
            self.shared.async_handle().spawn(async move {
                if let Err(err) = async_control
                    .filter_broadcast(
                        TargetSession::Filter(Box::new(move |id| light_client_peers.contains(id))),
                        SupportProtocols::LightClient.protocol_id(),
                        light_client_message.as_bytes(),
                    )
                    .await
                {
                    warn!("Broadcast last state to light client failed: {:?}", err);
                }
            });
```

**File:** sync/src/relayer/mod.rs (L745-792)
```rust
    let snapshot = shared.snapshot();
    let parent_chain_root = {
        let mmr = snapshot.chain_root_mmr(block.header().number() - 1);
        match mmr.get_root() {
            Ok(root) => root,
            Err(err) => {
                error_target!(
                    crate::LOG_TARGET_RELAY,
                    "Generate last state to light client failed: {:?}",
                    err
                );
                return;
            }
        }
    };

    let tip_header = packed::VerifiableHeader::new_builder()
        .header(block.header().data())
        .uncles_hash(block.calc_uncles_hash())
        .extension(Pack::pack(&block.extension()))
        .parent_chain_root(parent_chain_root)
        .build();
    let light_client_message = {
        let content = packed::SendLastState::new_builder()
            .last_header(tip_header)
            .build();
        packed::LightClientMessage::new_builder()
            .set(content)
            .build()
    };
    let light_client_peers: HashSet<PeerIndex> = nc
        .connected_peers()
        .into_iter()
        .filter_map(|index| nc.get_peer(index).map(|peer| (index, peer)))
        .filter(|(_id, peer)| peer.if_lightclient_subscribed)
        .map(|(id, _)| id)
        .collect();
    if let Err(err) = handle.block_on(nc.async_filter_broadcast_with_proto(
        SupportProtocols::LightClient.protocol_id(),
        TargetSession::Filter(Box::new(move |id| light_client_peers.contains(id))),
        light_client_message.as_bytes(),
    )) {
        debug_target!(
            crate::LOG_TARGET_RELAY,
            "relayer send last state to light client when accept block, error: {:?}",
            err,
        );
    }
```
