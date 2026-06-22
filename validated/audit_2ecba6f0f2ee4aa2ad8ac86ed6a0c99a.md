### Title
Unbounded `SendLastState` Broadcast Amplification via Uncapped Light Client Subscriptions — (`sync/src/relayer/mod.rs`, `util/light-client-protocol-server/src/components/get_last_state.rs`)

### Summary

An unprivileged remote attacker can open many inbound connections, send `GetLastState{subscribe:true}` on each, and cause the victim node to broadcast a full `SendLastState` message to every subscribed attacker peer on every new block — with no cap on the number of recipients. The compact-block relay path applies `MAX_RELAY_PEERS = 128`, but the light-client broadcast path has no equivalent limit.

### Finding Description

**Step 1 — Subscription with no guard.**

`GetLastStateProcess::execute` unconditionally sets `peer.if_lightclient_subscribed = true` for any peer that sends `GetLastState{subscribe:true}`. There is no rate limit, no per-IP cap, and no maximum subscriber count. [1](#0-0) 

**Step 2 — Uncapped broadcast on every accepted block.**

`build_and_broadcast_compact_block` (called from `Relayer::accept_block` on every verified block) iterates **all** connected peers, filters by `if_lightclient_subscribed`, and broadcasts `SendLastState` to every one of them. There is no `take(N)` or any other bound. [2](#0-1) 

Contrast this with the compact-block relay path in the same function, which explicitly caps recipients at `MAX_RELAY_PEERS = 128`: [3](#0-2) 

**Step 3 — Same uncapped path in `submit_block` RPC.**

The miner `submit_block` RPC contains an identical uncapped broadcast to all `if_lightclient_subscribed` peers. [4](#0-3) 

**Step 4 — No rate limiting on the LightClient protocol handler.**

Unlike the `Relayer`, `LightClientProtocol::try_process` has no rate limiter at all; every `GetLastState` message is processed unconditionally. [5](#0-4) 

### Impact Explanation

Each `SendLastState` message contains a full `VerifiableHeader` (header + uncles hash + extension + MMR root). With N attacker connections all subscribed, the victim node sends N such messages per block. At CKB's ~8 s block time and a typical `max_inbound` of several hundred connections, an attacker can sustain continuous amplified outbound traffic. This:

- Saturates the victim's outbound bandwidth.
- Delays or drops compact-block relay to honest peers, causing propagation latency and potential consensus deviation.
- Fills all inbound slots, preventing honest peers from connecting.

### Likelihood Explanation

The attack requires only a standard P2P connection and a single valid `GetLastState` message per session — no PoW, no keys, no privileged access. The LightClient protocol is enabled by default when the node is configured as a light-client server. The attacker needs no special knowledge beyond the protocol message format.

### Recommendation

1. **Cap subscribed light-client peers.** In `build_and_broadcast_compact_block` and `submit_block`, apply a `take(MAX_LIGHT_CLIENT_PEERS)` limit analogous to `MAX_RELAY_PEERS` for the compact-block path.
2. **Rate-limit `GetLastState` per peer.** Add a per-peer rate limiter in `LightClientProtocol::try_process` mirroring the one in `Relayer`.
3. **Cap total subscriptions.** Track a global counter of subscribed peers and reject `subscribe=true` once the limit is reached.

### Proof of Concept

```
1. Start a CKB node with LightClient protocol enabled.
2. Open N TCP connections to the node's P2P port.
3. On each connection, complete the p2p handshake and send:
       LightClientMessage { GetLastState { subscribe: true } }
4. Submit or relay one valid block to the victim node.
5. Observe: the victim sends N SendLastState messages outbound,
   one per subscribed attacker connection, with no cap.
6. Repeat per block; outbound bandwidth scales linearly with N.
```

The `if_lightclient_subscribed` flag is set per-peer with no global accounting, and the broadcast loop at lines 775–792 of `sync/src/relayer/mod.rs` has no upper bound on recipients. [6](#0-5) [2](#0-1)

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state.rs (L30-37)
```rust
        let subscribe: bool = self.message.subscribe().into();
        if subscribe {
            self.nc.with_peer_mut(
                self.peer,
                Box::new(|peer| {
                    peer.if_lightclient_subscribed = true;
                }),
            );
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

**File:** util/light-client-protocol-server/src/lib.rs (L96-107)
```rust
    async fn try_process(
        &mut self,
        nc: &Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        message: packed::LightClientMessageUnionReader<'_>,
    ) -> Status {
        match message {
            packed::LightClientMessageUnionReader::GetLastState(reader) => {
                components::GetLastStateProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
```

**File:** network/src/peer.rs (L86-88)
```rust
    /// Whether the remote peer is a light client, and it subscribes the chain state.
    pub if_lightclient_subscribed: bool,
}
```
