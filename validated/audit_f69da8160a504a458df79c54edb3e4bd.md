Based on my code analysis, here is my finding:

---

### Title
Sync Thread Indefinite Block via Saturated `bounded(24)` `process_block_tx` Channel — (`chain/src/init.rs`, `util/channel/src/lib.rs`)

### Summary

`ChainController::asynchronous_process_lonely_block` calls `Request::call_without_response`, which internally calls crossbeam's **blocking** `Sender::send()` on the `process_block_tx` channel. That channel is `bounded(24)`. If the channel is full, the calling sync/relay thread blocks indefinitely with no timeout or fallback, violating the invariant that the P2P message loop must remain responsive.

### Finding Description

**Root cause — blocking send on a bounded channel:**

`util/channel/src/lib.rs` lines 44–49:
```rust
pub fn call_without_response(sender: &Sender<Request<A, R>>, arguments: A) {
    let (responder, _response) = oneshot::channel();
    let _ = sender.send(Request { responder, arguments });
}
``` [1](#0-0) 

`sender` is a `crossbeam_channel::Sender`. `crossbeam_channel::Sender::send()` on a **bounded** channel **blocks** until a slot is available. The `let _ =` prefix only discards the `Result`; it does not make the call non-blocking.

**Channel capacity — bounded(24):**

`chain/src/init.rs` line 93:
```rust
let (process_block_tx, process_block_rx) = channel::bounded(24);
``` [2](#0-1) 

**Call site in the sync thread:**

`chain/src/chain_controller.rs` lines 61–63:
```rust
pub fn asynchronous_process_lonely_block(&self, lonely_block: LonelyBlock) {
    Request::call_without_response(&self.process_block_sender, lonely_block);
}
``` [3](#0-2) 

`sync/src/synchronizer/block_process.rs` calls `asynchronous_process_lonely_block` (confirmed by grep) from within the sync message-processing loop. If that call blocks, the entire sync thread stalls and can no longer service any P2P message from any peer.

### Impact Explanation

A stalled sync thread means the node stops processing all incoming P2P messages (block announcements, headers, transactions, ping/pong). From the network's perspective the node becomes unresponsive, causing peer disconnections and effective network isolation for the duration of the stall. This matches the stated scoped impact: **CKB network congestion / node isolation**.

### Likelihood Explanation

An attacker does **not** need to mine blocks themselves. They only need to relay 24 valid blocks (observable on the public network, e.g. from a fork or rapid succession of tip blocks) to the victim node faster than `ConsumeUnverifiedBlocks` can drain the pipeline. The `preload_unverified_block` thread adds an additional buffering stage (`bounded(128)` for `unverified_block_tx`), but the first bottleneck is the `bounded(24)` `process_block_tx` channel fed by `ChainService::start_process_block`. A well-connected attacker acting as a fast relay can saturate this in seconds during periods of high block production or chain reorganization. [4](#0-3) 

### Recommendation

Replace the blocking `send()` in `call_without_response` with `try_send()` and drop the message (with a warning log) if the channel is full, or increase the channel capacity and add a per-call deadline. The sync/relay path must never block on a downstream bounded channel.

### Proof of Concept

1. Spawn `build_chain_services` with a slow verifier mock (e.g., `thread::sleep(1s)` per block).
2. From a test thread, call `chain_controller.asynchronous_process_lonely_block(lonely_block)` 24 times in rapid succession — all succeed immediately (channel has 24 slots).
3. Call it a 25th time from a second thread that simulates the sync handler.
4. Assert the second thread does not return within a 500 ms timeout — it will stall indefinitely, confirming the blocking `send()` on the full `bounded(24)` channel. [2](#0-1) [1](#0-0)

### Citations

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

**File:** chain/src/init.rs (L49-53)
```rust
    let (preload_unverified_tx, preload_unverified_rx) =
        channel::bounded::<LonelyBlockHash>(BLOCK_DOWNLOAD_WINDOW as usize * 10);

    let (unverified_queue_stop_tx, unverified_queue_stop_rx) = ckb_channel::bounded::<()>(1);
    let (unverified_block_tx, unverified_block_rx) = channel::bounded::<UnverifiedBlock>(128usize);
```

**File:** chain/src/init.rs (L93-93)
```rust
    let (process_block_tx, process_block_rx) = channel::bounded(24);
```

**File:** chain/src/chain_controller.rs (L61-63)
```rust
    pub fn asynchronous_process_lonely_block(&self, lonely_block: LonelyBlock) {
        Request::call_without_response(&self.process_block_sender, lonely_block);
    }
```
