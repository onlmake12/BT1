### Title
Silently Discarded Return Values of `suspend_chunk_process` / `continue_chunk_process` Allow Tx-Pool Chunk Processor to Desynchronize During Block Verification — (File: `chain/src/verify.rs`)

---

### Summary

In `chain/src/verify.rs`, the `ConsumeUnverifiedBlocks::start()` loop wraps every call to `tx_pool_controller.suspend_chunk_process()` and `tx_pool_controller.continue_chunk_process()` in `let _ = …`, explicitly discarding the `Result`. If either call fails, execution continues as though it succeeded. This is the direct Rust analog of the Solidity `transfer()` unchecked-return-value pattern: a critical coordination operation whose failure is silently swallowed.

---

### Finding Description

`ConsumeUnverifiedBlocks::start()` is the thread that performs contextual block verification and chain-tip advancement. Before processing each unverified block it must pause the tx-pool's chunk processor (which concurrently validates pending transactions against the current snapshot), and resume it afterward. The relevant lines are:

```rust
// chain/src/verify.rs – lines 81, 101, 110, 112
let _ = self.tx_pool_controller.suspend_chunk_process();
// … block verification / reorg …
let _ = self.tx_pool_controller.continue_chunk_process();
``` [1](#0-0) [2](#0-1) [3](#0-2) 

Both functions communicate with the tx-pool service over a bounded channel. The `send_notify!` macro used internally by `TxPoolController` calls `try_send`, which returns `TrySendError::Full` when the channel is at capacity, and `TrySendError::Closed` when the service has shut down. [4](#0-3) 

Because the `Result` is discarded with `let _ =`, neither error variant is observed or acted upon.

---

### Impact Explanation

**Scenario A — `suspend_chunk_process` fails silently:**
The tx-pool chunk processor continues running concurrently while `consume_unverified_blocks` executes a reorg: it detaches old blocks, attaches new ones, and updates the shared snapshot. The chunk processor is simultaneously resolving and verifying pending transactions against the *old* snapshot. After the reorg completes, `update_tx_pool_for_reorg` is called to reconcile the pool, but any chunk-verification decisions made against the stale snapshot during the window are already committed to the pool's internal state. Transactions that spent outputs now detached from the main chain may be incorrectly retained; transactions valid only under the new tip may be incorrectly evicted. A miner using this node's block template could include invalid transactions or miss valid fee-paying ones. [5](#0-4) 

**Scenario B — `continue_chunk_process` fails silently:**
The chunk processor remains suspended after block verification ends. No further pending transactions are verified in chunks until the *next* block triggers another `suspend`/`continue` cycle. If the channel remains full across multiple blocks (e.g., under sustained load), the chunk processor stays suspended indefinitely. The tx-pool stops admitting new transactions that require chunk-based verification, effectively halting transaction propagation and block-template construction — a node-level DoS reachable by any peer that can keep the service channel saturated.

---

### Likelihood Explanation

The tx-pool service channel is bounded (capacity set by `DEFAULT_CHANNEL_SIZE = 32` in `util/channel/src/lib.rs`). [6](#0-5) 

An unprivileged attacker reachable via the RPC `send_transaction` endpoint or the P2P relay path can submit a burst of transactions. Each submission enqueues a message on the same service channel. If the channel is full at the moment a new block arrives and triggers `suspend_chunk_process` or `continue_chunk_process`, the send returns `TrySendError::Full`, which is silently dropped. This is a timing-dependent but realistic condition under normal network load, and trivially achievable under deliberate flooding.

---

### Recommendation

Replace the `let _ =` discards with explicit error handling:

```rust
if let Err(e) = self.tx_pool_controller.suspend_chunk_process() {
    error!("Failed to suspend tx-pool chunk process before block verification: {}", e);
    // Either abort verification or proceed with a logged warning,
    // depending on acceptable degradation policy.
}
// … verification …
if let Err(e) = self.tx_pool_controller.continue_chunk_process() {
    error!("Failed to resume tx-pool chunk process after block verification: {}", e);
    // Attempt retry or trigger a controlled restart of the chunk processor.
}
```

At minimum, errors should be logged so operators can detect the desynchronization. Ideally, a failed `suspend` should abort or delay block processing until the pool can be safely paused, and a failed `continue` should be retried.

---

### Proof of Concept

1. Connect to a CKB node's RPC endpoint as an unprivileged user.
2. Flood `send_transaction` with a large batch of valid (or even invalid-but-parseable) transactions to saturate the tx-pool service channel (capacity 32).
3. Simultaneously relay a valid block to the node via the P2P sync protocol, triggering `ConsumeUnverifiedBlocks::start()` to call `suspend_chunk_process()`.
4. Because the channel is full, `try_send` returns `TrySendError::Full`; `let _ =` discards it; the chunk processor is never suspended.
5. Block verification and reorg proceed concurrently with chunk processing against the pre-reorg snapshot.
6. After the reorg, `update_tx_pool_for_reorg` is called, but the pool's internal chunk-verification state is already inconsistent with the new chain tip.
7. For Scenario B: repeat step 2 timed to coincide with the `continue_chunk_process` call; the chunk processor remains suspended; subsequent transaction submissions are never chunk-verified; the node's block template omits all transactions requiring chunk verification.

### Citations

**File:** chain/src/verify.rs (L81-81)
```rust
                        let _ = self.tx_pool_controller.suspend_chunk_process();
```

**File:** chain/src/verify.rs (L101-101)
```rust
                        let _ = self.tx_pool_controller.continue_chunk_process();
```

**File:** chain/src/verify.rs (L110-112)
```rust
                        let _ = self.tx_pool_controller.suspend_chunk_process();
                        let _ = responder.send(self.processor.truncate(&target_tip_hash));
                        let _ = self.tx_pool_controller.continue_chunk_process();
```

**File:** chain/src/verify.rs (L386-398)
```rust
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

**File:** tx-pool/src/service.rs (L188-198)
```rust
macro_rules! send_notify {
    ($self:ident, $msg_type:ident, $args:expr) => {{
        let notify = Notify::new($args);
        $self
            .sender
            .try_send(Message::$msg_type(notify))
            .map_err(|e| {
                let (_m, e) = handle_try_send_error(e);
                e.into()
            })
    }};
```

**File:** util/channel/src/lib.rs (L22-22)
```rust
pub const DEFAULT_CHANNEL_SIZE: usize = 32;
```
