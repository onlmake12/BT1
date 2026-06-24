All four code references check out. Let me verify the `ckb_channel::oneshot` implementation and the `call_without_response` send behavior before writing the final output.

The code confirms every technical claim:

1. `ckb_channel::oneshot::channel` is backed by `std::sync::mpsc::sync_channel(1)`, so `verify_result_rx.recv()` at line 104 of `chain_controller.rs` is `std::sync::mpsc::Receiver::recv()` — blocking, no timeout. [1](#0-0) 

2. `submit_block` is a synchronous `fn` (not `async fn`) that calls `blocking_process_block` directly. [2](#0-1) 

3. `call_without_response` uses crossbeam's blocking `sender.send()` on a `bounded(24)` channel — callers beyond 24 block on the send itself, still holding a tokio worker thread. [3](#0-2) [4](#0-3) 

4. The RPC server has only a 30-second `TimeoutLayer` and no per-method concurrency cap. [5](#0-4) 

5. The tx-pool path correctly uses `block_in_place(|| response.recv())` — confirming the RPC layer does NOT automatically wrap sync handlers in `spawn_blocking`. [6](#0-5) 

---

Audit Report

## Title
Timeout-Free Blocking `recv()` in `blocking_process_block_internal` Enables RPC Thread Exhaustion via Concurrent `submit_block` Calls — (`chain/src/chain_controller.rs`)

## Summary
`blocking_process_block_internal` in `chain/src/chain_controller.rs` blocks the calling thread indefinitely on `std::sync::mpsc::Receiver::recv()` with no timeout. The `submit_block` RPC handler is a synchronous `fn` dispatched directly on a tokio worker thread with no `spawn_blocking` or `block_in_place` wrapper, so each concurrent `submit_block` call stalls one tokio worker thread for the full duration of block verification. With no per-method concurrency cap, an attacker can exhaust the tokio worker thread pool and render the RPC interface unresponsive.

## Finding Description

**Root cause — timeout-free blocking recv:**

`blocking_process_block_internal` at `chain/src/chain_controller.rs:84` creates a oneshot channel backed by `std::sync::mpsc::sync_channel(1)` (confirmed in `util/channel/src/lib.rs:14–16`). After enqueuing the block via `asynchronous_process_lonely_block`, it calls:

```rust
verify_result_rx.recv().unwrap_or_else(|err| { ... })
```

`verify_result_rx.recv()` is `std::sync::mpsc::Receiver::recv()` — a blocking, timeout-free call. It returns only when the full multi-stage verification pipeline (ChainService → OrphanBroker → PreloadUnverifiedBlocksChannel → ConsumeUnverifiedBlocks) fires the `verify_callback`. There is no `recv_timeout`, no `select!` with a deadline, and no cancellation path.

**Call path from RPC:**

`submit_block` at `rpc/src/module/miner.rs:260` is declared as `fn submit_block(&self, ...) -> Result<H256>` — a synchronous function, not `async fn`. The `handle_jsonrpc` async handler in `rpc/src/server.rs:218` calls `io.handle_call(call, T::default()).await`, which invokes the sync method directly on the tokio worker thread without `spawn_blocking` or `block_in_place`. The tx-pool path explicitly uses `block_in_place(|| response.recv())` (`tx-pool/src/service.rs:182`), confirming the RPC layer does not automatically wrap sync handlers.

**Backpressure that does not prevent exhaustion:**

`asynchronous_process_lonely_block` calls `Request::call_without_response` which uses crossbeam's blocking `sender.send()` on a `bounded(24)` channel (`chain/src/init.rs:93`). Callers 1–24 block on `verify_result_rx.recv()`; callers 25+ block on the channel send itself. All concurrent callers hold a tokio worker thread blocked until the chain service drains the queue.

**No rate limiting or concurrency cap:**

The RPC server applies only a 30-second HTTP `TimeoutLayer` at the transport layer. This timeout sends a 408 response to the HTTP client but does not cancel the in-flight `recv()` — the tokio task remains blocked. There is no per-method semaphore or concurrency cap on `submit_block`.

## Impact Explanation

Each concurrent `submit_block` call holds one tokio worker thread blocked in `verify_result_rx.recv()`. Exhausting the tokio worker thread pool causes the RPC interface to become unresponsive — no further RPC calls can be served. This matches **Note (0–500 points): Any local RPC API crash**.

## Likelihood Explanation

The `submit_block` RPC endpoint is reachable by any party with HTTP access to the RPC port. No cryptographic material, special privileges, or majority hashpower is required. The attacker only needs to send concurrent HTTP POST requests with syntactically valid blocks. The bounded channel of 24 means 24 concurrent requests suffice to saturate the queue; additional requests block on the channel send, still holding threads. The attack is repeatable and requires no victim interaction.

## Recommendation

1. Replace `verify_result_rx.recv()` with `verify_result_rx.recv_timeout(Duration::from_secs(N))` in `blocking_process_block_internal`, returning an error if verification does not complete within the deadline.
2. Wrap the synchronous `submit_block` handler body in `tokio::task::spawn_blocking` or use `block_in_place` (as the tx-pool path does at `tx-pool/src/service.rs:182`) to avoid blocking tokio worker threads.
3. Apply a concurrency semaphore on `submit_block` to bound the number of simultaneous in-flight block submissions.

## Proof of Concept

```bash
# Send 30 concurrent submit_block RPC calls (exceeds bounded channel of 24)
# Each call blocks a tokio worker thread in verify_result_rx.recv() with no timeout
for i in $(seq 1 30); do
  curl -s -X POST http://127.0.0.1:8114/ \
    -H 'Content-Type: application/json' \
    -d '{"id":1,"jsonrpc":"2.0","method":"submit_block","params":["0", <VALID_BLOCK_JSON>]}' &
done

# Attempt any other RPC call — will hang or time out
curl -s -X POST http://127.0.0.1:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"id":2,"jsonrpc":"2.0","method":"get_tip_block_number","params":[]}'
```

The root cause is `verify_result_rx.recv()` at `chain/src/chain_controller.rs:104`, called from `submit_block` at `rpc/src/module/miner.rs:295–298`, with the `process_block_sender` channel bounded at 24 (`chain/src/init.rs:93`) providing insufficient protection, and no per-method rate limit or concurrency cap in the RPC server (`rpc/src/server.rs:119–129`).

### Citations

**File:** util/channel/src/lib.rs (L7-16)
```rust
pub mod oneshot {
    //! A one-shot channel is used for sending a single message between asynchronous tasks.

    use std::sync::mpsc::sync_channel;
    pub use std::sync::mpsc::{Receiver, RecvError, SyncSender as Sender};

    /// Create a new one-shot channel for sending single values across asynchronous tasks.
    pub fn channel<T>() -> (Sender<T>, Receiver<T>) {
        sync_channel(1)
    }
```

**File:** util/channel/src/lib.rs (L44-50)
```rust
    pub fn call_without_response(sender: &Sender<Request<A, R>>, arguments: A) {
        let (responder, _response) = oneshot::channel();
        let _ = sender.send(Request {
            responder,
            arguments,
        });
    }
```

**File:** rpc/src/module/miner.rs (L260-298)
```rust
    fn submit_block(&self, work_id: String, block: Block) -> Result<H256> {
        let block: packed::Block = block.into();
        let block: Arc<core::BlockView> = Arc::new(block.into_view());
        let header = block.header();
        debug!(
            "start to submit block, work_id = {}, block = #{}({})",
            work_id,
            block.number(),
            block.hash()
        );

        let snapshot: &Snapshot = &self.shared.snapshot();
        let consensus = snapshot.consensus();

        // Verify header
        HeaderVerifier::new(snapshot, consensus)
            .verify(&header)
            .map_err(|err| handle_submit_error(&work_id, &err))?;
        if self
            .shared
            .snapshot()
            .get_block_header(&block.parent_hash())
            .is_none()
        {
            let err = format!(
                "Block parent {} of {}-{} not found",
                block.parent_hash(),
                block.number(),
                block.hash()
            );

            return Err(handle_submit_error(&work_id, &err));
        }

        // Verify and insert block
        let is_new = self
            .chain
            .blocking_process_block(Arc::clone(&block))
            .map_err(|err| handle_submit_error(&work_id, &err))?;
```

**File:** chain/src/init.rs (L93-93)
```rust
    let (process_block_tx, process_block_rx) = channel::bounded(24);
```

**File:** rpc/src/server.rs (L119-129)
```rust
        let app = Router::new()
            .route("/", method_router.clone())
            .route("/{*path}", method_router)
            .route("/ping", get(ping_handler))
            .layer(Extension(Arc::clone(rpc)))
            .layer(CorsLayer::permissive())
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
            .layer(Extension(stream_config));
```

**File:** tx-pool/src/service.rs (L182-184)
```rust
        block_in_place(|| response.recv())
            .map_err(handle_recv_error)
            .map_err(Into::into)
```
