### Title
Light Client Protocol `SendLastState` Not Pushed When Blocks Arrive via Sync Protocol — (`sync/src/relayer/mod.rs`, `chain/src/verify.rs`)

### Summary

The CKB light client protocol allows a light client peer to subscribe to automatic chain-tip push notifications by sending `GetLastState { subscribe: true }`. The server records the subscription and is expected to push `SendLastState` every time the chain tip changes. However, `SendLastState` is only emitted in two code paths — the relay protocol and the miner RPC — and is entirely absent from the sync protocol's block-processing path. This is a direct structural analog to the ERC-1155 `URI` event omission: the server acknowledges the subscription (analogous to `supportsInterface` returning `true`) but silently fails to emit the required push message when state changes through a different entry point.

---

### Finding Description

**Subscription acknowledgement (the "claim to support"):**

When a light client peer sends `GetLastState { subscribe: true }`, `GetLastStateProcess::execute` sets `peer.if_lightclient_subscribed = true` and replies with the current tip. [1](#0-0) 

The `subscribe` field in the schema is documented as:
> "Whether the server is requested to push the state automatically." [2](#0-1) 

**Where `SendLastState` IS sent (relay path):**

`build_and_broadcast_compact_block` in the relayer sends `SendLastState` to every peer whose `if_lightclient_subscribed` flag is set, whenever a compact block is accepted. [3](#0-2) 

**Where `SendLastState` IS sent (miner RPC path):**

`submit_block` in the miner RPC also sends `SendLastState` to subscribed light clients after a new block is accepted. [4](#0-3) 

**Where `SendLastState` is MISSING (sync protocol path):**

When a block arrives via the sync protocol (e.g., during IBD, catch-up after downtime, or reorg gap-filling), the chain verifier calls `notify_new_block` — which only notifies internal RPC subscribers — but never sends `SendLastState` to subscribed light client peers. [5](#0-4) 

There is no `SendLastState` broadcast anywhere in this code path. The `notify_new_block` call feeds the `NotifyController` pub/sub system for JSON-RPC topics (`new_tip_header`, `new_tip_block`), which is entirely separate from the P2P light client protocol. [6](#0-5) 

---

### Impact Explanation

A light client peer that sends `GetLastState { subscribe: true }` to a full node that is currently catching up via the sync protocol (post-downtime, during IBD, or during a reorg) will receive the initial tip reply but will receive **no further `SendLastState` pushes** for any blocks processed through that path. The light client's view of the chain tip becomes stale for the entire duration of the sync-protocol catch-up. Any security decisions the light client makes based on its tip (transaction finality, proof verification anchoring) will be based on incorrect state. The light client has no way to distinguish "no new blocks" from "server is not sending updates."

---

### Likelihood Explanation

The scenario is reachable by any unprivileged light client peer. A full node that has been offline for even a short period will re-enter the sync protocol path to catch up. During that window, all subscribed light clients are silently starved of updates. The `LIGHT_CLIENT` flag is advertised in the identify protocol, so light clients can discover and connect to such nodes without any privileged access. [7](#0-6) 

---

### Recommendation

In `chain/src/verify.rs`, after `notify_new_block` is called for a new best block, add a broadcast of `SendLastState` to all peers whose `if_lightclient_subscribed` flag is set, mirroring the logic already present in `build_and_broadcast_compact_block` and `submit_block`. The `Shared` handle and `NetworkController` are both available in the chain verifier context. Alternatively, the `NotifyController::notify_new_block` subscriber in the relay layer could be extended to also push `SendLastState`, but the cleanest fix is at the canonical tip-change site in `chain/src/verify.rs`.

---

### Proof of Concept

1. Start a CKB full node that is behind by N blocks (e.g., was offline).
2. Connect a light client peer and send `GetLastState { subscribe: true }`.
3. The server replies with `SendLastState` for the current (stale) tip and sets `if_lightclient_subscribed = true`.
4. The full node begins catching up via the sync protocol (`SendBlock` messages processed through `chain/src/verify.rs`).
5. Observe: the light client receives **zero** `SendLastState` messages during the entire catch-up, even though the chain tip advances by N blocks.
6. Compare with the relay path: once the node is fully synced and a new block arrives via compact block relay, `SendLastState` is correctly pushed.

The root cause is the absence of any `SendLastState` broadcast in `chain/src/verify.rs` lines 361–408, contrasted with the explicit broadcasts at `sync/src/relayer/mod.rs` lines 761–792 and `rpc/src/module/miner.rs` lines 340–367.

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state.rs (L29-55)
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

        let tip_header = match self.protocol.get_verifiable_tip_header() {
            Ok(tip_state) => tip_state,
            Err(errmsg) => {
                return StatusCode::InternalError.with_context(errmsg);
            }
        };

        let content = packed::SendLastState::new_builder()
            .last_header(tip_header)
            .build();
        let message = packed::LightClientMessage::new_builder()
            .set(content)
            .build();

        self.nc.reply(self.peer, &message).await
    }
```

**File:** util/gen-types/schemas/extensions.mol (L314-317)
```text
table GetLastState {
    // Whether the server is requested to push the state automatically.
    subscribe:                  Bool,
}
```

**File:** sync/src/relayer/mod.rs (L761-792)
```rust
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

**File:** rpc/src/module/miner.rs (L340-367)
```rust
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

**File:** chain/src/verify.rs (L361-408)
```rust
        if new_best_block {
            let tip_header = block.header();
            info!(
                "block: {}, hash: {:#x}, epoch: {:#}, total_diff: {:#x}, txs: {}, proposals: {}",
                tip_header.number(),
                tip_header.hash(),
                tip_header.epoch(),
                cannon_total_difficulty,
                block.transactions().len(),
                block.data().proposals().len()
            );

            self.update_proposal_table(&fork);
            let (detached_proposal_id, new_proposals) = self
                .proposal_table
                .finalize(origin_proposals, tip_header.number());
            fork.detached_proposal_id = detached_proposal_id;

            let new_snapshot =
                self.shared
                    .new_snapshot(tip_header, cannon_total_difficulty, epoch, new_proposals);

            self.shared.store_snapshot(Arc::clone(&new_snapshot));

            let tx_pool_controller = self.shared.tx_pool_controller();
            if tx_pool_controller.service_started() {
                if let Err(e) = tx_pool_controller.update_tx_pool_for_reorg(
                    fork.detached_blocks().clone(),
                    fork.attached_blocks().clone(),
                    fork.detached_proposal_id().clone(),
                    new_snapshot,
                ) {
                    error!("[verify block] notify update_tx_pool_for_reorg error {}", e);
                }
                if let Err(e) = tx_pool_controller.update_ibd_state(in_ibd) {
                    error!("Notify update_ibd_state error {}", e);
                }
            }

            self.shared
                .notify_controller()
                .notify_new_block(block.to_owned());
            if log_enabled!(ckb_logger::Level::Trace) {
                self.print_chain(10);
            }
            if let Some(metrics) = ckb_metrics::handle() {
                metrics.ckb_chain_tip.set(block.header().number() as i64);
            }
```

**File:** notify/src/lib.rs (L110-125)
```rust
pub struct NotifyController {
    new_block_register: NotifyRegister<BlockView>,
    new_block_watcher: NotifyWatcher<Byte32>,
    new_block_notifier: Sender<BlockView>,
    new_transaction_register: NotifyRegister<PoolTransactionEntry>,
    new_transaction_notifier: Sender<PoolTransactionEntry>,
    proposed_transaction_register: NotifyRegister<PoolTransactionEntry>,
    proposed_transaction_notifier: Sender<PoolTransactionEntry>,
    reject_transaction_register: NotifyRegister<(PoolTransactionEntry, Reject)>,
    reject_transaction_notifier: Sender<(PoolTransactionEntry, Reject)>,
    network_alert_register: NotifyRegister<Alert>,
    network_alert_notifier: Sender<Alert>,
    log_register: NotifyRegister<LogEntry>,
    log_notifier: Sender<LogEntry>,
    handle: Handle,
}
```

**File:** network/src/protocols/identify/mod.rs (L564-580)
```rust
bitflags::bitflags! {
    /// Node Function Identification
    #[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
    pub struct Flags: u64 {
        /// Compatibility reserved
        const COMPATIBILITY = 0b1;
        /// Discovery protocol, which can provide peers data service
        const DISCOVERY = 0b10;
        /// Sync protocol can provide Block and Header download service
        const SYNC = 0b100;
        /// Relay protocol, which can provide CompactBlock and Transaction broadcast/forwarding services
        const RELAY = 0b1000;
        /// Light client protocol, which can provide Block / Transaction data and existence-proof services
        const LIGHT_CLIENT = 0b10000;
        /// Client-side block filter protocol can provide BlockFilter download service
        const BLOCK_FILTER = 0b100000;
    }
```
