### Title
RPC Server Activated Before TxPool Service Initialization Completes, Allowing Pre-Startup Callers to Block RPC Handler Threads — (`ckb-bin/src/subcommand/run.rs`)

---

### Summary

In `ckb-bin/src/subcommand/run.rs`, the RPC server (including the Pool RPC module) is fully started and accepting connections via `start_network_and_rpc()` **before** `tx_pool_builder.start(network_controller)` is called. This creates a window where the `TxPoolController` is reachable through the RPC but the backing async service has not yet started. Pool RPC methods do not check `service_started()` before dispatching messages, so any caller hitting this window causes an RPC handler thread to block indefinitely on `block_in_place(|| response.recv())` until the service eventually starts.

---

### Finding Description

`TxPoolServiceBuilder` has a two-phase initialization:

**Phase 1 — `TxPoolServiceBuilder::new()`** (called inside `SharedBuilder::build()`):
Creates the `TxPoolController` with `started = Arc::new(AtomicBool::new(false))`. The controller is stored in `Shared` and immediately accessible via `shared.tx_pool_controller()`. No async consumer task is running yet.

**Phase 2 — `TxPoolServiceBuilder::start(network_controller)`**:
Spawns the async message-processing tasks and, only after all tasks are spawned, sets `self.started.store(true, Ordering::Release)`.

In `run()`, the ordering is:

```
line 69-74: launcher.start_network_and_rpc(...)   // RPC server starts, begins accepting connections
line 76:    let tx_pool_builder = pack.take_tx_pool_builder();
line 77:    tx_pool_builder.start(network_controller); // service starts HERE
```

`start_network_and_rpc` calls `RpcServer::new(...)`, which calls `handler.block_on(rx_addr)` — it **blocks until the TCP listener is bound and ready**. The RPC server is therefore fully live and accepting connections before `tx_pool_builder.start()` is called.

Pool RPC methods such as `send_transaction`, `tx_pool_info`, `get_raw_tx_pool`, `test_tx_pool_accept`, `remove_transaction`, `get_pool_tx_detail_info`, `clear_tx_pool`, and `clear_tx_verify_queue` all call `send_message!` without first checking `service_started()`:

```rust
// rpc/src/module/pool.rs  line 612-635
fn send_transaction(&self, tx: Transaction, ...) -> Result<H256> {
    let tx_pool = self.shared.tx_pool_controller();
    let submit_tx = tx_pool.submit_local_tx(tx.clone()); // no service_started() guard
    ...
}
```

The `send_message!` macro:

```rust
macro_rules! send_message {
    ($self:ident, $msg_type:ident, $args:expr) => {{
        let (responder, response) = oneshot::channel();
        let request = Request::call($args, responder);
        $self.sender.try_send(Message::$msg_type(request))...?;
        block_in_place(|| response.recv())   // blocks current thread waiting for response
            .map_err(handle_recv_error)...
    }};
}
```

`try_send` succeeds as long as the channel has capacity (512 slots). The message is queued. `block_in_place(|| response.recv())` then blocks the calling RPC handler thread indefinitely because no consumer task is running to process the message and send a response.

The only method that correctly guards itself is `tx_pool_ready()`, which explicitly calls `service_started()` and returns a boolean without dispatching a message.

---

### Impact Explanation

An unprivileged RPC caller who sends any pool-touching RPC request (e.g., `send_transaction`, `tx_pool_info`) during the startup window will cause the RPC handler thread to block inside `block_in_place`. The RPC thread pool size is `max(system_parallelism, 1)` (e.g., 4 threads on a 4-core machine). If an attacker sends enough concurrent requests during the window, all RPC handler threads become blocked, preventing any further RPC responses until the tx pool service starts and drains the queue. This constitutes a temporary RPC-layer denial of service during node startup. Additionally, if the channel fills to its 512-message capacity before the service starts, subsequent callers receive an internal error rather than a clean "not ready" response.

---

### Likelihood Explanation

The window between `start_network_and_rpc()` returning and `tx_pool_builder.start()` completing is small (microseconds to low milliseconds on typical hardware). However, the RPC server is provably live before `start()` is called — `RpcServer::new` blocks until the TCP listener is bound. A local or fast-network RPC caller who monitors the port and immediately sends requests upon connection success can reliably hit this window. The attack requires no credentials, no special privileges, and no knowledge of internal state — only the ability to connect to the RPC port.

---

### Recommendation

1. **Reorder startup**: Call `tx_pool_builder.start(network_controller)` before `start_network_and_rpc()`, or pass the `NetworkController` to the builder without requiring it to be started first, so the service is fully ready before the RPC port opens.
2. **Add a readiness guard**: In all pool RPC methods, check `tx_pool.service_started()` and return a structured "service not ready" error immediately, consistent with how `tx_pool_ready()` already works.
3. **Document the invariant**: The comment in `chain/src/init.rs` distinguishes `build_chain_services` from `start_chain_services` for exactly this reason. A similar explicit contract should exist for `TxPoolServiceBuilder`.

---

### Proof of Concept

```
# 1. Start ckb node
ckb run &

# 2. Poll until the RPC port is open (TCP SYN-ACK received)
while ! nc -z 127.0.0.1 8114; do sleep 0.0001; done

# 3. Immediately send a pool RPC call — hits the window before tx_pool_builder.start()
curl -s -d '{"id":1,"jsonrpc":"2.0","method":"tx_pool_info","params":[]}' \
     -H 'content-type:application/json' http://127.0.0.1:8114

# Result: the curl call hangs until the tx pool service finishes starting,
# blocking one RPC handler thread. Repeat with N concurrent calls to exhaust
# the thread pool (N = CPU core count).
```

**Root cause chain:**
`run()` (`ckb-bin/src/subcommand/run.rs:69-77`) → `RpcServer::new` binds and listens → `tx_pool_builder.start()` not yet called → `TxPoolController::started = false` → `send_transaction` RPC → `tx_pool.submit_local_tx()` → `send_message!(SubmitLocalTx)` → `try_send` succeeds (channel has capacity) → `block_in_place(|| response.recv())` blocks forever (no consumer running). [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

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

**File:** tx-pool/src/service.rs (L171-185)
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
```

**File:** tx-pool/src/service.rs (L516-524)
```rust
        let started = Arc::new(AtomicBool::new(false));

        let controller = TxPoolController {
            sender,
            reorg_sender,
            handle: handle.clone(),
            chunk_tx: Arc::new(chunk_tx),
            started: Arc::clone(&started),
        };
```

**File:** tx-pool/src/service.rs (L730-736)
```rust
            }
        });
        self.started.store(true, Ordering::Release);
        if let Err(err) = self.tx_pool_controller.load_persisted_data(txs) {
            error!("Failed to import persistent txs, cause: {}", err);
        }
    }
```

**File:** rpc/src/module/pool.rs (L607-635)
```rust
    fn tx_pool_ready(&self) -> Result<bool> {
        let tx_pool = self.shared.tx_pool_controller();
        Ok(tx_pool.service_started())
    }

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

**File:** util/launcher/src/lib.rs (L511-571)
```rust
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

**File:** rpc/src/server.rs (L59-68)
```rust
        let http_address = Self::start_server(
            &rpc,
            config.listen_address.to_owned(),
            handler.clone(),
            false,
        )
        .inspect(|&local_addr| {
            info!("Listen HTTP RPCServer on address: {}", local_addr);
        })
        .unwrap();
```

**File:** rpc/src/server.rs (L150-153)
```rust
        });

        let rx_addr = handler.block_on(rx_addr)?;
        Ok(rx_addr)
```
