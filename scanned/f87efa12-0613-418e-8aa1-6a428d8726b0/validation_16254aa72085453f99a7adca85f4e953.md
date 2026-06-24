Audit Report

## Title
RPC Pool Methods Callable Before `TxPoolService` Initialization, Blocking Handler Threads — (`ckb-bin/src/subcommand/run.rs`, `tx-pool/src/service.rs`, `rpc/src/module/pool.rs`)

## Summary

In `ckb-bin/src/subcommand/run.rs`, the RPC server is started via `start_network_and_rpc` before `tx_pool_builder.start()` is called, creating a window during which all pool RPC methods (`send_transaction`, `tx_pool_info`, `clear_tx_pool`, etc.) are reachable but the tx-pool service loop has not yet been spawned. Each such call enqueues a message into the mpsc channel and then blocks the handler thread indefinitely in `block_in_place(|| response.recv())` until the service loop starts and drains the queue. Flooding the RPC endpoint during this window with enough concurrent requests blocks all Tokio worker threads in the RPC runtime, rendering the RPC server unresponsive for the duration of the startup window.

## Finding Description

**Startup ordering** in `ckb-bin/src/subcommand/run.rs` (L69–77):

```rust
let network_controller = launcher.start_network_and_rpc(...); // RPC port is LIVE
let tx_pool_builder = pack.take_tx_pool_builder();
tx_pool_builder.start(network_controller);                     // service loop spawned HERE
```

`start_network_and_rpc` binds the HTTP/WS/TCP RPC ports and returns before `tx_pool_builder.start()` is called. [1](#0-0) 

**`TxPoolServiceBuilder::start()` sets `started = true` only after spawning all async tasks** at line 732, and the `started` flag is initialized to `false` at line 516. [2](#0-1) [3](#0-2) 

**The `send_message!` macro** (L171–186) uses `block_in_place(|| response.recv())` with no timeout. The `responder` half of the oneshot channel lives inside the `Request` sitting in the mpsc channel; `response.recv()` therefore blocks until the service loop processes the message. [4](#0-3) 

**Pool RPC handlers do not call `service_started()` before invoking `send_message!`**. For example, `send_transaction` (L612–635), `tx_pool_info` (L671–682), and `clear_tx_pool` (L684–692) all proceed directly to the tx-pool controller without any readiness guard. [5](#0-4) [6](#0-5) 

**The only `service_started()` call in the RPC layer** is in `tx_pool_ready` (L607–610), which is a dedicated readiness probe — not a guard on the other methods. [7](#0-6) 

**By contrast, `chain/src/verify.rs` correctly guards its tx-pool calls** with `service_started()` before calling `update_tx_pool_for_reorg` or `update_ibd_state`. [8](#0-7) 

**The mpsc channel has capacity 512** (`DEFAULT_CHANNEL_SIZE`), so `try_send` succeeds silently during the startup window, and the message (with its `responder`) sits in the queue. [9](#0-8) 

The RPC runtime is a Tokio multi-thread runtime with `rpc_threads_num` worker threads (= `max(system_parallelism, 1)`). Each `block_in_place` call occupies one worker thread. The HTTP server has a 30-second `TimeoutLayer`, but this operates at the HTTP response level and does not cancel the already-executing `block_in_place` closure — the thread remains blocked until the service loop starts. [10](#0-9) 

## Impact Explanation

During the startup window, an attacker sending as few as `rpc_threads_num` concurrent pool RPC calls blocks all Tokio worker threads in the RPC runtime. No new async tasks can be scheduled, making the RPC server completely unresponsive to legitimate callers for the remainder of the startup window. This matches the allowed impact: **Note (0–500 points) — Any local RPC API crash/unresponsiveness**, with potential escalation to **High** if the RPC bind address is exposed beyond localhost.

## Likelihood Explanation

The attack requires only the ability to reach the RPC port (local by default, but configurable to be public) and knowledge that the node is restarting — both trivially observable. Node restarts are routine. The startup window is deterministic and reproducible on every restart. No credentials, keys, or special protocol knowledge are required. The number of concurrent requests needed equals the CPU core count of the target machine, which is easily achievable.

## Recommendation

**Preferred:** Reorder startup in `ckb-bin/src/subcommand/run.rs` so that `tx_pool_builder.start(network_controller)` is called before `start_network_and_rpc`, ensuring the service loop is ready before any external caller can reach the RPC port.

**Alternative:** Add a `service_started()` guard at the top of each pool RPC handler (analogous to `tx_pool_ready` and the existing guard in `chain/src/verify.rs`), returning an explicit "service not ready" error immediately rather than blocking.

## Proof of Concept

1. Start a CKB node (`ckb run`).
2. Poll the RPC port with TCP connect until it accepts connections.
3. Before the log line indicating tx-pool readiness, send `N` concurrent HTTP POST requests to any pool method (e.g., `tx_pool_info`) where `N ≥ rpc_threads_num`.
4. Each request enters `block_in_place(|| response.recv())` and blocks a worker thread.
5. All RPC worker threads are now blocked; subsequent legitimate RPC calls (e.g., `get_tip_block_number`) receive no response until `tx_pool_builder.start()` completes and the service loop drains the queued messages.

### Citations

**File:** ckb-bin/src/subcommand/run.rs (L69-77)
```rust
    let network_controller = launcher.start_network_and_rpc(
        &shared,
        chain_controller,
        miner_enable,
        pack.take_relay_tx_receiver(),
    );

    let tx_pool_builder = pack.take_tx_pool_builder();
    tx_pool_builder.start(network_controller);
```

**File:** tx-pool/src/service.rs (L53-53)
```rust
pub(crate) const DEFAULT_CHANNEL_SIZE: usize = 512;
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

**File:** tx-pool/src/service.rs (L516-516)
```rust
        let started = Arc::new(AtomicBool::new(false));
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

**File:** rpc/src/server.rs (L125-128)
```rust
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
```
