Audit Report

## Title
RPC Server Accepts Connections Before TxPool Service Starts, Causing Handler Thread Blocking — (`ckb-bin/src/subcommand/run.rs`)

## Summary
In `run.rs`, `start_network_and_rpc()` fully binds the TCP listener and returns before `tx_pool_builder.start(network_controller)` is called. Pool RPC methods dispatch messages via `send_message!` without checking `service_started()`, causing the calling RPC handler thread to block in `block_in_place(|| response.recv())` until the tx pool service starts. If enough concurrent requests arrive during this window, all RPC handler threads can be occupied, making the RPC temporarily unresponsive.

## Finding Description
**Phase ordering in `run.rs`:**
Lines 69–77 confirm the sequence: `start_network_and_rpc()` is called first, which internally calls `RpcServer::new()` → `start_server()` → `handler.block_on(rx_addr)` (server.rs line 152), blocking until the TCP listener is bound and live. Only after `start_network_and_rpc()` returns is `tx_pool_builder.start(network_controller)` called on line 77.

**`TxPoolServiceBuilder::start()` window:**
`start()` (service.rs line 573) performs synchronous disk I/O (`tx_pool.load_from_file()` at line 581) before spawning async tasks, then sets `self.started.store(true, Ordering::Release)` at line 732 only after all tasks are spawned. The `started` flag is initialized to `false` at construction (line 516). The window spans from TCP bind to line 732 — which includes file I/O and task spawning, potentially hundreds of milliseconds with a large persisted tx pool.

**`send_message!` blocks without a guard:**
The macro (service.rs lines 171–185) calls `try_send` (succeeds while the 512-slot channel has capacity) then `block_in_place(|| response.recv())`. No consumer task is running yet, so `response.recv()` blocks the OS thread indefinitely until the service starts and drains the queue. `tx_pool_ready()` (pool.rs line 607–609) is the only pool RPC method that calls `service_started()` before acting; all other pool methods — `send_transaction`, `tx_pool_info`, `get_raw_tx_pool`, `test_tx_pool_accept`, `remove_transaction`, `get_pool_tx_detail_info`, `clear_tx_pool`, `clear_tx_verify_queue` — dispatch directly without this guard.

**Existing mitigations are insufficient:**
The `TimeoutLayer` (server.rs lines 125–128, 30-second timeout) cancels the HTTP future but cannot unblock the OS thread already inside `block_in_place`. The thread remains blocked until the service starts regardless of the HTTP timeout.

## Impact Explanation
This matches **Note (0–500 points): Any local RPC API crash**. During the startup window, concurrent pool RPC calls occupy all `rpc_threads_num` (= `max(system_parallelism, 1)`) OS threads in the RPC runtime inside `block_in_place`. With all threads blocked, the RPC server cannot schedule new tasks, making it temporarily unresponsive. The effect is self-resolving once `tx_pool_builder.start()` completes, but constitutes a transient RPC-layer denial of service. If the 512-message channel fills before the service starts, subsequent callers receive an internal error rather than a clean "not ready" response.

## Likelihood Explanation
The window is bounded by synchronous disk I/O in `load_from_file()` plus async task spawning — potentially hundreds of milliseconds on nodes with large persisted tx pools. A local caller polling the port and immediately sending concurrent requests upon TCP connection success can reliably hit this window. No credentials or privileges are required. The attack is repeatable on every node restart.

## Recommendation
1. **Reorder startup**: Call `tx_pool_builder.start(network_controller)` before `start_network_and_rpc()`. The `NetworkController` is already available at that point (returned from the network start step), so no structural change is needed — only the call order in `run.rs` lines 69–77 needs to be swapped.
2. **Add a readiness guard**: In all pool RPC methods, check `tx_pool.service_started()` and return a structured "service not ready" error immediately, consistent with how `tx_pool_ready()` already works (pool.rs line 607–609).

## Proof of Concept
```bash
# 1. Start ckb node (with a non-empty persisted tx pool to widen the window)
ckb run &

# 2. Poll until RPC port opens
while ! nc -z 127.0.0.1 8114; do sleep 0.0001; done

# 3. Send N concurrent pool RPC calls immediately (N = CPU core count)
for i in $(seq 1 $(nproc)); do
  curl -s -d '{"id":1,"jsonrpc":"2.0","method":"tx_pool_info","params":[]}' \
       -H 'content-type:application/json' http://127.0.0.1:8114 &
done
wait

# Result: all N curl calls hang until tx_pool_builder.start() completes,
# blocking all RPC handler threads. Any RPC call during this period
# (including non-pool calls) cannot be scheduled until threads free up.
```

**Root cause chain:** `run.rs:69–74` (`start_network_and_rpc`) → `server.rs:152` (`block_on(rx_addr)`, TCP listener live) → `run.rs:77` (`tx_pool_builder.start()` not yet called) → `service.rs:516` (`started = false`) → `pool.rs:622–623` (`submit_local_tx`, no `service_started()` guard) → `service.rs:177` (`try_send` succeeds, channel has capacity) → `service.rs:182` (`block_in_place(|| response.recv())` blocks OS thread, no consumer running).