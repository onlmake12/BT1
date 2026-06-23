### Title
RPC Pool Methods Callable Before `TxPoolService` Initialization Completes, Causing Handler Thread Exhaustion — (`tx-pool/src/service.rs`, `ckb-bin/src/subcommand/run.rs`, `rpc/src/module/pool.rs`)

---

### Summary

In `ckb-bin/src/subcommand/run.rs`, the RPC server (and P2P network) is started **before** `tx_pool_builder.start()` is called. During this startup window, all pool-related RPC methods (`send_transaction`, `tx_pool_info`, `test_tx_pool_accept`, `remove_transaction`, `clear_tx_pool`, `get_raw_tx_pool`, `get_pool_tx_detail_info`) can be invoked by any RPC caller. These methods use the blocking `send_message!` macro, which enqueues a request and then calls `block_in_place(|| response.recv())` — blocking the handler thread indefinitely until the tx-pool service loop processes the message. Since the service loop has not yet been spawned, the thread hangs for the entire startup window, and concurrent requests from an unprivileged RPC caller can exhaust the RPC thread pool.

---

### Finding Description

**Startup ordering in `run.rs`:**

```
start_network_and_rpc(...)   // ← RPC server is LIVE, accepting connections
tx_pool_builder.start(...)   // ← tx-pool service loop spawned HERE
``` [1](#0-0) 

`start_network_and_rpc` builds and starts the RPC HTTP/WS/TCP server and the P2P network service, returning a `NetworkController`. Only after this returns does `tx_pool_builder.start(network_controller)` run. [2](#0-1) 

**`TxPoolServiceBuilder::start()` sets `started = true` only after spawning async tasks:** [3](#0-2) [4](#0-3) 

**The `send_message!` macro blocks on `response.recv()` with no timeout:** [5](#0-4) 

**Pool RPC methods do not check `service_started()` before calling `send_message!`:** [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8) 

The only method that checks `service_started()` is `tx_pool_ready`, which is a dedicated readiness probe — not a guard on the other methods. [10](#0-9) 

By contrast, the chain verifier **does** guard its tx-pool calls with `service_started()`: [11](#0-10) 

The `TxPoolController` exposes `service_started()` precisely for this purpose, but pool RPC handlers do not use it. [12](#0-11) 

The channel has capacity 512, so `try_send` succeeds silently during the window: [13](#0-12) 

---

### Impact Explanation

During the startup window (from when the RPC server binds its port to when `tx_pool_builder.start()` completes), any unprivileged RPC caller can send pool requests. Each such request causes a handler thread to block in `block_in_place(|| response.recv())` with no timeout. Flooding the RPC endpoint with `send_transaction`, `tx_pool_info`, or `clear_tx_pool` calls during this window exhausts the RPC thread pool, preventing legitimate requests from being served. The window is bounded by startup time (including disk I/O for persisted tx loading), but is reliably reproducible on every node restart. Additionally, the P2P relay layer (`Relayer`) calls `submit_remote_tx` and `fetch_txs_with_cycles` on the tx-pool controller without a `service_started()` guard, so a peer connecting during startup and sending relay messages triggers the same blocking behavior. [14](#0-13) [15](#0-14) 

---

### Likelihood Explanation

The vulnerability is reachable by any unprivileged RPC caller (local or remote, depending on RPC bind configuration) or any P2P peer that connects during node startup. Node restarts are routine (upgrades, crashes, maintenance). The startup window is deterministic and observable (the RPC port opens before the tx-pool is ready). No special privileges, keys, or majority hashpower are required.

---

### Recommendation

1. **Preferred:** Reorder startup in `run.rs` so that `tx_pool_builder.start()` is called before `start_network_and_rpc`, ensuring the service is ready before any external caller can reach it.

2. **Alternative:** Add a `service_started()` guard at the top of each pool RPC handler method, returning an explicit "service not ready" error (analogous to what `tx_pool_ready` already exposes), consistent with how `chain/src/verify.rs` guards its tx-pool calls.

---

### Proof of Concept

1. Start a CKB node (`ckb run`).
2. Immediately after the RPC port opens (observable via TCP connect), before the log line indicating tx-pool is ready, send concurrent `send_transaction` or `tx_pool_info` RPC calls in a tight loop.
3. Each call enters `block_in_place(|| response.recv())` and blocks a handler thread.
4. With enough concurrent calls (≤ 512, the channel capacity), all RPC threads are blocked and the node is unresponsive to legitimate RPC requests for the remainder of the startup window.

The root cause is the ordering:

```
// run.rs
let network_controller = launcher.start_network_and_rpc(...); // RPC live, tx-pool NOT started
// ... window of vulnerability ...
tx_pool_builder.start(network_controller);                     // tx-pool starts HERE
``` [16](#0-15)

### Citations

**File:** ckb-bin/src/subcommand/run.rs (L64-77)
```rust
    let chain_controller =
        launcher.start_chain_service(&shared, pack.take_chain_services_builder());

    launcher.start_block_filter(&shared);

    let network_controller = launcher.start_network_and_rpc(
        &shared,
        chain_controller,
        miner_enable,
        pack.take_relay_tx_receiver(),
    );

    let tx_pool_builder = pack.take_tx_pool_builder();
    tx_pool_builder.start(network_controller);
```

**File:** util/launcher/src/lib.rs (L497-571)
```rust
        let network_controller = NetworkService::new(
            Arc::clone(&network_state),
            protocols,
            required_protocol_ids,
            (
                shared.consensus().identify_name(),
                self.version.to_string(),
                flags,
            ),
            TransportType::Tcp,
        )
        .start(shared.async_handle())
        .expect("Start network service failed");

        let rpc_config = self.adjust_rpc_config();
        let mut builder = ServiceBuilder::new(&rpc_config)
            .enable_chain(shared.clone())
            .enable_pool(
                shared.clone(),
                rpc_config
                    .extra_well_known_lock_scripts
                    .iter()
                    .map(|script| script.clone().into())
                    .collect(),
                rpc_config
                    .extra_well_known_type_scripts
                    .iter()
                    .map(|script| script.clone().into())
                    .collect(),
            )
            .enable_miner(
                shared.clone(),
                network_controller.clone(),
                chain_controller.clone(),
                miner_enable,
            )
            .enable_net(
                network_controller.clone(),
                sync_shared,
                Arc::new(chain_controller.clone()),
            )
            .enable_stats(shared.clone(), Arc::clone(&alert_notifier))
            .enable_experiment(shared.clone())
            .enable_integration_test(
                shared.clone(),
                network_controller.clone(),
                chain_controller,
                rpc_config
                    .extra_well_known_lock_scripts
                    .iter()
                    .map(|script| script.clone().into())
                    .collect(),
                rpc_config
                    .extra_well_known_type_scripts
                    .iter()
                    .map(|script| script.clone().into())
                    .collect(),
            )
            .enable_alert(
                alert_verifier,
                alert_notifier,
                network_controller.clone(),
                shared.clone(),
            )
            .enable_terminal(shared.clone(), network_controller.clone())
            .enable_indexer(
                shared.clone(),
                &self.args.config.db,
                &self.args.config.indexer,
            )
            .enable_debug();
        builder.enable_subscription(shared.clone());
        let io_handler = builder.build();

        let _rpc = RpcServer::new(rpc_config, io_handler, self.rpc_handle.clone());
```

**File:** tx-pool/src/service.rs (L171-186)
```rust
macro_rules! send_message {
    ($self:ident, $msg_type:ident, $args:expr) => {{
        let (responder, response) = oneshot::channel();
        let request = Request::call($args, responder);
        $self
            .sender
            .try_send(Message::$msg_type(request))
            .map_err(|e| {
                let (_m, e) = handle_try_send_error(e);
                e
            })?;
        block_in_place(|| response.recv())
            .map_err(handle_recv_error)
            .map_err(Into::into)
    }};
}
```

**File:** tx-pool/src/service.rs (L201-205)
```rust
impl TxPoolController {
    /// Return whether tx-pool service is started
    pub fn service_started(&self) -> bool {
        self.started.load(Ordering::Acquire)
    }
```

**File:** tx-pool/src/service.rs (L278-285)
```rust
    pub async fn submit_remote_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Cycle,
        peer: PeerIndex,
    ) -> Result<(), AnyError> {
        send_message!(self, SubmitRemoteTx, (tx, declared_cycles, peer))
    }
```

**File:** tx-pool/src/service.rs (L356-368)
```rust
    /// Return txs with cycles
    /// Mainly for relay transactions
    pub async fn fetch_txs_with_cycles(
        &self,
        short_ids: HashSet<ProposalShortId>,
    ) -> Result<FetchTxsWithCyclesResult, AnyError> {
        let (responder, response) = tokio::sync::oneshot::channel();
        let request = AsyncRequest::call(short_ids, responder);
        self.sender
            .send(Message::FetchTxsWithCycles(request))
            .await?;
        response.await.map_err(Into::into)
    }
```

**File:** tx-pool/src/service.rs (L511-516)
```rust
        let (sender, receiver) = mpsc::channel(DEFAULT_CHANNEL_SIZE);
        let block_assembler_channel = mpsc::channel(BLOCK_ASSEMBLER_CHANNEL_SIZE);
        let (reorg_sender, reorg_receiver) = mpsc::channel(DEFAULT_CHANNEL_SIZE);
        let signal_receiver: CancellationToken = new_tokio_exit_rx();
        let (chunk_tx, chunk_rx) = watch::channel(ChunkCommand::Resume);
        let started = Arc::new(AtomicBool::new(false));
```

**File:** tx-pool/src/service.rs (L573-608)
```rust
    pub fn start(self, network: NetworkController) {
        let consensus = self.snapshot.cloned_consensus();

        let verify_queue = Arc::new(RwLock::new(VerifyQueue::new(
            self.tx_pool_config.max_tx_verify_cycles,
        )));

        let tx_pool = TxPool::new(self.tx_pool_config, self.snapshot);
        let txs = match tx_pool.load_from_file() {
            Ok(txs) => txs,
            Err(e) => {
                error!("{}", e.to_string());
                error!("Failed to load txs from tx-pool persistent data file, all txs are ignored");
                Vec::new()
            }
        };

        let (block_assembler_sender, mut block_assembler_receiver) = self.block_assembler_channel;
        let service = TxPoolService {
            tx_pool_config: Arc::new(tx_pool.config.clone()),
            tx_pool: Arc::new(RwLock::new(tx_pool)),
            orphan: Arc::new(RwLock::new(OrphanPool::new())),
            block_assembler: self.block_assembler,
            txs_verify_cache: self.txs_verify_cache,
            callbacks: Arc::new(self.callbacks),
            tx_relay_sender: self.tx_relay_sender,
            block_assembler_sender,
            verify_queue: Arc::clone(&verify_queue),
            network,
            consensus,
            fee_estimator: self.fee_estimator,
        };

        let mut verify_mgr =
            VerifyMgr::new(service.clone(), self.chunk_rx, self.signal_receiver.clone());
        self.handle.spawn(async move { verify_mgr.run().await });
```

**File:** tx-pool/src/service.rs (L730-735)
```rust
            }
        });
        self.started.store(true, Ordering::Release);
        if let Err(err) = self.tx_pool_controller.load_persisted_data(txs) {
            error!("Failed to import persistent txs, cause: {}", err);
        }
```

**File:** rpc/src/module/pool.rs (L607-610)
```rust
    fn tx_pool_ready(&self) -> Result<bool> {
        let tx_pool = self.shared.tx_pool_controller();
        Ok(tx_pool.service_started())
    }
```

**File:** rpc/src/module/pool.rs (L612-635)
```rust
    fn send_transaction(
        &self,
        tx: Transaction,
        outputs_validator: Option<OutputsValidator>,
    ) -> Result<H256> {
        let tx: packed::Transaction = tx.into();
        let tx: core::TransactionView = tx.into_view();

        self.check_output_validator(outputs_validator, &tx)?;

        let tx_pool = self.shared.tx_pool_controller();
        let submit_tx = tx_pool.submit_local_tx(tx.clone());

        if let Err(e) = submit_tx {
            error!("Send submit_tx request error {}", e);
            return Err(RPCError::ckb_internal_error(e));
        }

        let tx_hash = tx.hash();
        match submit_tx.unwrap() {
            Ok(_) => Ok(tx_hash.into()),
            Err(reject) => Err(RPCError::from_submit_transaction_reject(&reject)),
        }
    }
```

**File:** rpc/src/module/pool.rs (L671-682)
```rust
    fn tx_pool_info(&self) -> Result<TxPoolInfo> {
        let tx_pool = self.shared.tx_pool_controller();
        let get_tx_pool_info = tx_pool.get_tx_pool_info();
        if let Err(e) = get_tx_pool_info {
            error!("Send get_tx_pool_info request error {}", e);
            return Err(RPCError::ckb_internal_error(e));
        };

        let tx_pool_info = get_tx_pool_info.unwrap();

        Ok(tx_pool_info.into())
    }
```

**File:** rpc/src/module/pool.rs (L684-701)
```rust
    fn clear_tx_pool(&self) -> Result<()> {
        let snapshot = Arc::clone(&self.shared.snapshot());
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_pool(snapshot)
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
    }

    fn clear_tx_verify_queue(&self) -> Result<()> {
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_verify_queue()
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
    }
```

**File:** rpc/src/module/pool.rs (L703-718)
```rust
    fn get_raw_tx_pool(&self, verbose: Option<bool>) -> Result<RawTxPool> {
        let tx_pool = self.shared.tx_pool_controller();

        let raw = if verbose.unwrap_or(false) {
            let info = tx_pool
                .get_all_entry_info()
                .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?;
            RawTxPool::Verbose(info.into())
        } else {
            let ids = tx_pool
                .get_all_ids()
                .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?;
            RawTxPool::Ids(ids.into())
        };
        Ok(raw)
    }
```

**File:** chain/src/verify.rs (L385-398)
```rust
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
```
