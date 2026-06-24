Audit Report

## Title
RPC Server Accepts Pool Requests Before TxPool Service Starts, Blocking Handler Threads — (`ckb-bin/src/subcommand/run.rs`)

## Summary
In `run()`, `start_network_and_rpc()` binds and opens the HTTP/TCP RPC listener before returning, while `tx_pool_builder.start()` — which spawns the async consumer tasks and sets `started = true` — is called on the next line. Pool RPC methods that use `send_message!` enqueue a request and then block the calling thread on `block_in_place(|| response.recv())` with no `service_started()` guard, causing the thread to hang until the consumer tasks drain the queue. Sending enough concurrent requests during this startup window exhausts the RPC thread pool.

## Finding Description
The ordering in `ckb-bin/src/subcommand/run.rs` lines 69–77 is confirmed:

```rust
let network_controller = launcher.start_network_and_rpc(...); // RPC live here
let tx_pool_builder = pack.take_tx_pool_builder();
tx_pool_builder.start(network_controller);                     // service starts here
```

`start_network_and_rpc` calls `RpcServer::new` at `util/launcher/src/lib.rs` line 571. Inside `RpcServer::new` (`rpc/src/server.rs` lines 59–68), `Self::start_server` is called and `handler.block_on(rx_addr)` at line 152 blocks until the TCP listener is fully bound. The RPC port is therefore accepting connections before `start_network_and_rpc` returns.

`TxPoolServiceBuilder::new()` initializes `started = Arc::new(AtomicBool::new(false))` at `tx-pool/src/service.rs` line 516. `TxPoolServiceBuilder::start()` sets `self.started.store(true, Ordering::Release)` at line 732, only after all async tasks are spawned.

The `send_message!` macro at `tx-pool/src/service.rs` lines 171–185 calls `try_send` (which succeeds as long as the channel has capacity) and then `block_in_place(|| response.recv())` with no `service_started()` guard. `submit_local_tx` at line 262 uses this macro directly. `send_transaction` in `rpc/src/module/pool.rs` lines 622–623 calls `tx_pool.submit_local_tx(tx.clone())` without any readiness check. The only guarded method is `tx_pool_ready()` at lines 607–610, which explicitly calls `service_started()` and returns without dispatching a message.

Since no consumer task is running yet, the oneshot `response` channel will not receive a reply until `tx_pool_builder.start()` spawns the consumer tasks and they begin draining the queue. Every RPC handler thread that hits this path blocks until that happens.

## Impact Explanation
An unprivileged caller who sends any pool-touching RPC request (`send_transaction`, `tx_pool_info`, `get_raw_tx_pool`, etc.) during the startup window causes one RPC handler thread to block. With enough concurrent requests equal to the thread pool size (`max(system_parallelism, 1)`), all handler threads are blocked, making the RPC API temporarily unresponsive. This is a transient, self-resolving local RPC API unavailability during node startup, matching the **Note (0–500 points): Any local RPC API crash** impact category.

## Likelihood Explanation
The window between `RpcServer::new` returning and `tx_pool_builder.start()` completing is microseconds to low milliseconds. A caller monitoring the port and sending requests immediately upon TCP connection success can reliably hit this window. No credentials or privileges are required. The attack is repeatable on every node restart.

## Recommendation
1. **Reorder startup**: Call `tx_pool_builder.start(network_controller)` before `start_network_and_rpc()`, ensuring the service is fully ready before the RPC port opens.
2. **Add readiness guards**: In all pool RPC methods, check `tx_pool.service_started()` and return a structured "service not ready" error immediately, consistent with how `tx_pool_ready()` already works.

## Proof of Concept
```bash
# 1. Start ckb node
ckb run &

# 2. Poll until RPC port is open
while ! nc -z 127.0.0.1 8114; do sleep 0.0001; done

# 3. Send N concurrent pool RPC calls (N = CPU core count)
for i in $(seq 1 $(nproc)); do
  curl -s -d '{"id":1,"jsonrpc":"2.0","method":"tx_pool_info","params":[]}' \
       -H 'content-type:application/json' http://127.0.0.1:8114 &
done
wait
# Result: all N curl calls hang until tx_pool_builder.start() completes,
# exhausting the RPC thread pool and blocking all further RPC responses.
```