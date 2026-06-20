### Title
Unbounded Light-Client Subscriber Broadcast Per Block Acceptance — (`util/light-client-protocol-server/src/components/get_last_state.rs`, `sync/src/relayer/mod.rs`, `rpc/src/module/miner.rs`)

---

### Summary

Any peer connected on the LightClient protocol can send a single `GetLastState{subscribe:true}` message to permanently set `if_lightclient_subscribed = true` on its peer record. There is no cap on how many peers may hold this flag. Every subsequent block accepted by the node triggers an O(n\_subscribers) `SendLastState` broadcast in both the P2P relay path (`build_and_broadcast_compact_block`) and the miner `submit_block` RPC path, with no equivalent of the `MAX_RELAY_PEERS` guard that protects the regular compact-block relay path.

---

### Finding Description

**Entry point — subscription with no guard:**

`GetLastStateProcess::execute` unconditionally sets the flag for any peer that sends `subscribe = true`: [1](#0-0) 

There is no check on how many peers are already subscribed, no rate-limit, and no authentication requirement beyond being a connected peer on the LightClient protocol.

**Broadcast path 1 — P2P relay (`build_and_broadcast_compact_block`):**

On every accepted block, the function collects *all* connected peers whose `if_lightclient_subscribed` is `true` and broadcasts to every one of them: [2](#0-1) 

Compare this with the regular compact-block relay path in the same function, which is explicitly capped: [3](#0-2) 

The `.take(MAX_RELAY_PEERS)` guard exists for normal peers but is entirely absent for the light-client subscriber broadcast.

**Broadcast path 2 — miner `submit_block` RPC:**

The same uncapped pattern is duplicated in the miner path: [4](#0-3) 

**No subscription limit anywhere in the codebase:**

A search for any cap (`max_light_client`, `subscribe.*limit`, `max.*subscri`) returns zero matches across the entire repository. [5](#0-4) 

The field is a plain `bool` on the `Peer` struct with no associated counter or global limit.

---

### Impact Explanation

An attacker who opens K inbound connections (bounded only by the node's general `max_inbound` peer limit, typically 125 by default) and sends one `GetLastState{subscribe:true}` per connection forces the node to perform K additional `SendLastState` serializations and socket writes on every accepted block — on both the P2P relay path and the miner RPC path. This:

- Saturates the async send queue with low-priority light-client messages during the critical block-propagation window.
- Delays `CompactBlock` delivery to honest full-node peers, increasing orphan-block risk.
- Requires zero PoW, zero stake, and zero ongoing cost from the attacker after the initial connections are established.

---

### Likelihood Explanation

The LightClient protocol is a production feature enabled by node operators. Inbound connections are accepted by default. The attack requires only a TCP connection and a single small P2P message per slot — well within reach of any adversary who wants to degrade a target node's block-propagation performance.

---

### Recommendation

1. **Add a global cap on subscribed light-client peers.** Maintain an atomic counter of currently-subscribed peers and reject (or silently ignore) `GetLastState{subscribe:true}` once the cap is reached, analogous to how `MAX_RELAY_PEERS` caps the compact-block relay fan-out.
2. **Apply `.take(MAX_LIGHT_CLIENT_SUBSCRIBERS)` in both broadcast sites** (`build_and_broadcast_compact_block` and `submit_block`) to mirror the existing guard on the regular relay path.
3. **Consider rate-limiting subscription requests** per peer or per IP to prevent rapid re-subscription after disconnection.

---

### Proof of Concept

```
1. Start a CKB node with LightClient protocol enabled.
2. Open K TCP connections; on each, complete the p2p handshake and send:
       GetLastState { subscribe: true }
3. Mine (or relay) one block to the node.
4. Observe: build_and_broadcast_compact_block iterates all K peers
   (sync/src/relayer/mod.rs:775-781) and calls async_filter_broadcast_with_proto
   for each; miner.rs:348-363 does the same on the submit_block path.
5. Measure wall-clock latency of CompactBlock delivery to an honest peer
   as K increases from 1 to max_inbound (~125).
   Expected: latency grows linearly with K; at K=125 the send queue is
   saturated before honest peers receive the compact block.
```

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state.rs (L30-38)
```rust
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

**File:** sync/src/relayer/mod.rs (L727-732)
```rust
    let selected_peers: Vec<PeerIndex> = nc
        .connected_peers()
        .into_iter()
        .filter(|target_peer| peer != *target_peer)
        .take(MAX_RELAY_PEERS)
        .collect();
```

**File:** sync/src/relayer/mod.rs (L775-792)
```rust
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

**File:** rpc/src/module/miner.rs (L348-367)
```rust
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

**File:** network/src/peer.rs (L86-88)
```rust
    /// Whether the remote peer is a light client, and it subscribes the chain state.
    pub if_lightclient_subscribed: bool,
}
```
