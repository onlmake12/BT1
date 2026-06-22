### Title
Missing `service_started()` Guard in `send_transaction` and Other Pool RPC Methods Allows Pre-Initialization Thread Exhaustion — (`File: rpc/src/module/pool.rs`)

---

### Summary

The `TxPoolController` exposes a `service_started()` readiness flag and a dedicated `tx_pool_ready()` RPC to check it, but the actual tx-pool RPC methods (`send_transaction`, `test_tx_pool_accept`, `remove_transaction`, `tx_pool_info`, `clear_tx_pool`, `clear_tx_verify_queue`, `get_raw_tx_pool`, `get_pool_tx_detail_info`) do **not** check `service_started()` before dispatching messages. Because the RPC server is started **before** the tx-pool service in the node startup sequence, an unprivileged RPC caller can reach these methods while the tx-pool is still uninitialized, causing the RPC handler thread to block indefinitely and exhausting the RPC thread pool.

---

### Finding Description

`TxPoolController` tracks initialization state via an `Arc<AtomicBool>` named `started`: [1](#0-0) 

The flag is set to `true` only after `TxPoolServiceBuilder::start()` completes its async loop setup: [2](#0-1) 

A dedicated `service_started()` accessor and a `tx_pool_ready()` RPC exist: [3](#0-2) [4](#0-3) 

However, `send_transaction` — and every other pool RPC method — proceeds directly to `submit_local_tx` without checking `service_started()`: [5](#0-4) 

The `send_message!` macro sends a message into the channel and then **blocks the calling thread** waiting for a response: [6](#0-5) 

If the service loop is not yet running, the response never arrives and the thread hangs indefinitely.

The startup sequence in `run.rs` starts the RPC server **before** the tx-pool service: [7](#0-6) 

This creates a window where the RPC port is open and accepting connections but `started` is still `false`.

---

### Impact Explanation

An unprivileged RPC caller who connects during the startup window and calls `send_transaction` (or any of the other unguarded pool methods) will cause the RPC handler thread to block indefinitely. With enough concurrent calls, all RPC threads are exhausted, rendering the node's RPC interface completely unresponsive for the duration of the attack. This is a denial-of-service against the RPC layer during node startup, which is a realistic and sensitive operational window (e.g., after a restart or upgrade).

**Impact: High** — complete RPC unavailability.

---

### Likelihood Explanation

The startup window is short but deterministic and observable (e.g., by monitoring when the TCP port opens). Any unprivileged user with RPC access (local or remote, depending on `listen_address` config) can exploit this. No authentication, key, or special privilege is required. The `tx_pool_ready()` RPC being a separate opt-in check rather than an enforced guard makes this easy to trigger accidentally or deliberately.

**Likelihood: Medium** — requires timing the startup window, but the window is predictable and the attack requires no special access.

---

### Recommendation

Add a `service_started()` guard at the top of each pool RPC method that dispatches to the tx-pool service. For example, in `send_transaction`:

```rust
fn send_transaction(&self, tx: Transaction, outputs_validator: Option<OutputsValidator>) -> Result<H256> {
    let tx_pool = self.shared.tx_pool_controller();
    if !tx_pool.service_started() {
        return Err(RPCError::custom(RPCError::Invalid, "tx-pool service is not started yet"));
    }
    // ... rest of the method
}
```

Apply the same guard to `test_tx_pool_accept`, `remove_transaction`, `tx_pool_info`, `clear_tx_pool`, `clear_tx_verify_queue`, `get_raw_tx_pool`, and `get_pool_tx_detail_info`. [8](#0-7) 

Alternatively, enforce the guard inside the `send_message!` macro itself so all future callers are protected by default.

---

### Proof of Concept

1. Start a CKB node (`ckb run`).
2. Immediately after the RPC TCP port opens (detectable via `connect()`), before `tx_pool_ready` returns `true`, send multiple concurrent `send_transaction` JSON-RPC calls.
3. Each call enters `send_message!`, enqueues a message, and blocks on `response.recv()`.
4. Since `TxPoolServiceBuilder::start()` has not yet set `started = true` and the service loop is not yet processing messages, no response is ever sent.
5. All RPC handler threads are blocked; subsequent RPC calls (including `tx_pool_ready`) time out or are refused.

```bash
# Race the startup window:
while ! nc -z 127.0.0.1 8114; do sleep 0.01; done
for i in $(seq 1 32); do
  curl -s -X POST http://127.0.0.1:8114 \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"send_transaction","params":[{"version":"0x0","cell_deps":[],"header_deps":[],"inputs":[],"outputs":[],"outputs_data":[],"witnesses":[]},null],"id":1}' &
done
# All threads now hang; node RPC is unresponsive
```

### Citations

**File:** tx-pool/src/service.rs (L163-169)
```rust
pub struct TxPoolController {
    sender: mpsc::Sender<Message>,
    reorg_sender: mpsc::Sender<Notify<ChainReorgArgs>>,
    chunk_tx: Arc<watch::Sender<ChunkCommand>>,
    handle: Handle,
    started: Arc<AtomicBool>,
}
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

**File:** tx-pool/src/service.rs (L202-205)
```rust
    /// Return whether tx-pool service is started
    pub fn service_started(&self) -> bool {
        self.started.load(Ordering::Acquire)
    }
```

**File:** tx-pool/src/service.rs (L732-732)
```rust
        self.started.store(true, Ordering::Release);
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

**File:** rpc/src/module/pool.rs (L637-700)
```rust
    fn test_tx_pool_accept(
        &self,
        tx: Transaction,
        outputs_validator: Option<OutputsValidator>,
    ) -> Result<EntryCompleted> {
        let tx: packed::Transaction = tx.into();
        let tx: core::TransactionView = tx.into_view();

        self.check_output_validator(outputs_validator, &tx)?;

        let tx_pool = self.shared.tx_pool_controller();

        let test_accept_tx_reslt = tx_pool.test_accept_tx(tx).map_err(|e| {
            error!("Send test_tx_pool_accept_tx request error {}", e);
            RPCError::ckb_internal_error(e)
        })?;

        test_accept_tx_reslt
            .map(|test_accept_result| test_accept_result.into())
            .map_err(|reject| {
                error!("Send test_tx_pool_accept_tx request error {}", reject);
                RPCError::from_submit_transaction_reject(&reject)
            })
    }

    fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
        let tx_pool = self.shared.tx_pool_controller();

        tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
            error!("Send remove_tx request error {}", e);
            RPCError::ckb_internal_error(e)
        })
    }

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
```

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
