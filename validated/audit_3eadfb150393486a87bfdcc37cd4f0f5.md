Audit Report

## Title
RPC Pool Methods Block Tokio Worker Threads Before `TxPoolService` Initialization — (`ckb-bin/src/subcommand/run.rs`, `tx-pool/src/service.rs`, `rpc/src/module/pool.rs`)

## Summary
In `ckb-bin/src/subcommand/run.rs`, `start_network_and_rpc` binds and opens the RPC port before `tx_pool_builder.start()` spawns the tx-pool service loop. During this window, pool RPC handlers invoke `send_message!`, which calls `block_in_place(|| response.recv())` with no timeout, permanently blocking each Tokio worker thread until the service loop drains the queued message. Flooding the RPC with enough concurrent pool calls during this window exhausts all worker threads and renders the RPC server unresponsive.

## Finding Description
**Startup ordering** (`ckb-bin/src/subcommand/run.rs` L69–77): `start_network_and_rpc` returns with the RPC port live at L74; `tx_pool_builder.start(network_controller)` is not called until L77. The `started` flag is initialized to `false` at `tx-pool/src/service.rs` L516 and only set to `true` at L732, after all async tasks are spawned.

**Blocking receive** (`tx-pool/src/service.rs` L171–186): The `send_message!` macro calls `try_send` into the 512-capacity mpsc channel (L53), then immediately calls `block_in_place(|| response.recv())` with no timeout. The `responder` half of the oneshot channel lives inside the queued `Request`; `response.recv()` blocks the calling thread until the service loop processes the message.

**No readiness guard in pool RPC handlers** (`rpc/src/module/pool.rs`): `send_transaction` (L612–635), `tx_pool_info` (L671–682), and `clear_tx_pool` (L684–692) all proceed directly to `tx_pool_controller` methods that invoke `send_message!` without first calling `service_started()`. The only `service_started()` call in the RPC layer is in the dedicated probe `tx_pool_ready` (L607–610), which is not a guard on other methods.

**Contrast with chain layer** (`chain/src/verify.rs` L385–398): `update_tx_pool_for_reorg` and `update_ibd_state` are correctly gated behind `if tx_pool_controller.service_started()`, demonstrating the intended pattern is known but not applied to RPC handlers.

**TimeoutLayer does not rescue blocked threads** (`rpc/src/server.rs` L125–128): The 30-second `TimeoutLayer` races the async future against a timer and returns a 408 response when the timer wins, but the `block_in_place` closure is synchronous and continues holding the OS thread until `response.recv()` returns. The thread is not released by the timeout.

**Exploit flow**: During the window between L74 and L77 of `run.rs`, an attacker sends `N ≥ rpc_threads_num` concurrent HTTP POST requests to any pool method. Each request enters `block_in_place(|| response.recv())` and occupies one Tokio worker thread. With all worker threads blocked, no new async tasks can be scheduled; the RPC server is completely unresponsive to all callers until `tx_pool_builder.start()` completes and the service loop drains the queue.

## Impact Explanation
The concrete impact is complete local RPC unresponsiveness during the node startup window. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash/unresponsiveness**. The RPC binds to localhost by default; if configured to a public address, the impact escalates, but the base claim is scoped to the local default.

## Likelihood Explanation
The attack requires only local access to the RPC port (the default configuration) and the ability to observe that the node is restarting, which is trivially detectable via TCP connect polling. No credentials, keys, or protocol knowledge are required. The startup window is deterministic and reproducible on every node restart. The number of concurrent requests needed equals the CPU core count, which is easily achievable from a single machine.

## Recommendation
**Preferred:** Reorder startup in `ckb-bin/src/subcommand/run.rs` so that `tx_pool_builder.start(network_controller)` is called before `start_network_and_rpc`, ensuring the service loop is ready before any external caller can reach the RPC port.

**Alternative:** Add a `service_started()` guard at the top of each pool RPC handler (mirroring `tx_pool_ready` and the existing guard in `chain/src/verify.rs`), returning an explicit "service not ready" error immediately rather than entering `block_in_place`.

## Proof of Concept
1. Start a CKB node (`ckb run`).
2. Poll the RPC port with TCP connect until it accepts connections.
3. Before the log line indicating tx-pool readiness, send `N` concurrent HTTP POST requests to `tx_pool_info` (or any pool method) where `N ≥ rpc_threads_num` (= `max(CPU cores, 1)`).
4. Observe that each request enters `block_in_place(|| response.recv())` and blocks a worker thread.
5. Observe that subsequent RPC calls (e.g., `get_tip_block_number`) receive no response until `tx_pool_builder.start()` completes and the service loop drains the queued messages.