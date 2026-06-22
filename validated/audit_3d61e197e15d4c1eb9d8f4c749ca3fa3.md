### Title
Silently Ignored `suspend_chunk_process` / `continue_chunk_process` Return Values Allow Concurrent Tx-Pool Chunk Execution During Block Verification — (File: `chain/src/verify.rs`)

### Summary
In `ConsumeUnverifiedBlocks::start()`, the return values of `tx_pool_controller.suspend_chunk_process()` and `tx_pool_controller.continue_chunk_process()` are discarded with `let _ = ...`. If either call fails, block verification proceeds without the tx-pool chunk process being properly suspended, allowing concurrent script execution in both the block verifier and the tx-pool chunk verifier. This is directly analogous to the ERC20 unchecked-return-value class: an operation whose success is assumed but never confirmed.

### Finding Description
`ConsumeUnverifiedBlocks::start()` is the dedicated thread that performs contextual block verification. Before calling `consume_unverified_blocks`, it is supposed to pause the tx-pool's ongoing chunk-based script verification so that the two do not race over shared VM resources and snapshot state. After verification it resumes the chunk process.

```rust
// chain/src/verify.rs  lines 81, 101, 110–112
let _ = self.tx_pool_controller.suspend_chunk_process();   // ← return value dropped
...
self.processor.consume_unverified_blocks(unverified_task); // block verification runs
...
let _ = self.tx_pool_controller.continue_chunk_process();  // ← return value dropped
```

`suspend_chunk_process()` and `continue_chunk_process()` communicate with the tx-pool service over an internal channel and return a `Result`. If the channel is full, the service is shutting down, or any other transient error occurs, the call returns `Err(...)`. Because the result is bound to `_`, the error is silently discarded and block verification proceeds as if the suspension succeeded.

The same pattern appears for the `truncate` path at lines 110–112.

### Impact Explanation
When `suspend_chunk_process` fails silently, the tx-pool chunk verifier continues executing CKB-VM scripts concurrently with the block verifier. Both paths share the same `txs_verify_cache` (`Arc<RwLock<...>>`), the same `Snapshot`, and the same underlying RocksDB snapshot. Concurrent writes to the verify cache from two independent verification contexts can produce stale or incorrect cache entries that are later used to skip re-verification of transactions. A block containing a transaction whose cached cycle count was corrupted by the concurrent chunk run could be accepted with an incorrect cycle total, violating the consensus cycle limit. Conversely, a valid transaction in the chunk queue could be evicted or mis-cached, causing it to be incorrectly rejected after the block is committed.

### Likelihood Explanation
The tx-pool controller communicates over a bounded channel. Under any load spike — including one deliberately induced by a peer flooding the node with transactions — the channel can be momentarily full, causing `suspend_chunk_process` to return `Err`. A block/header relayer that sends a valid block at the same moment the tx-pool is saturated will trigger this path without any special privilege. No key material, majority hashpower, or social engineering is required.

### Recommendation
Propagate the error instead of discarding it. If suspension fails, either retry with a short back-off or abort processing the current unverified block and re-queue it:

```rust
// Instead of:
let _ = self.tx_pool_controller.suspend_chunk_process();

// Use:
if let Err(e) = self.tx_pool_controller.suspend_chunk_process() {
    error!("Failed to suspend chunk process before block verification: {}", e);
    // re-queue or skip this block rather than proceeding unsynchronised
    continue;
}
```

Apply the same treatment to `continue_chunk_process` and to the truncate path.

### Proof of Concept

1. Start a CKB node with a moderately active tx-pool (e.g., submit ~100 large-cycle transactions so the chunk verifier is continuously busy).
2. From a second peer, relay a valid block at the moment the chunk-process channel is saturated.
3. `suspend_chunk_process()` returns `Err` (channel full); the error is silently dropped.
4. `consume_unverified_blocks` runs concurrently with the chunk verifier.
5. Both paths write to `txs_verify_cache`; the cache entry for a transaction present in both the block and the chunk queue is overwritten with an inconsistent cycle count.
6. On the next block, the node reads the corrupted cache entry and skips re-verification, accepting a cycle total that violates the consensus limit — or rejects a valid transaction that was correctly verified by the chunk path.

Relevant code locations: [1](#0-0) [2](#0-1)

### Citations

**File:** chain/src/verify.rs (L71-102)
```rust
    pub(crate) fn start(mut self) {
        loop {
            let trace_begin_loop = minstant::Instant::now();
            select! {
                recv(self.unverified_block_rx) -> msg => match msg {
                    Ok(unverified_task) => {
                        // process this unverified block
                        if let Some(handle) = ckb_metrics::handle() {
                            handle.ckb_chain_consume_unverified_block_waiting_block_duration.observe(trace_begin_loop.elapsed().as_secs_f64())
                        }
                        let _ = self.tx_pool_controller.suspend_chunk_process();

                        let _trace_now = minstant::Instant::now();
                        let block_hash = unverified_task.block.hash();
                        let block_number = unverified_task.block.number();
                        if let Err(payload) = catch_unwind(AssertUnwindSafe(|| {
                            self.processor.consume_unverified_blocks(unverified_task);
                        })) {
                            error!(
                                "consume unverified block {}-{} panicked: {}",
                                block_number,
                                block_hash,
                                panic_payload_to_string(payload.as_ref())
                            );
                            self.processor.is_pending_verify.remove(&block_hash);
                        }
                        if let Some(handle) = ckb_metrics::handle() {
                            handle.ckb_chain_consume_unverified_block_duration.observe(_trace_now.elapsed().as_secs_f64())
                        }

                        let _ = self.tx_pool_controller.continue_chunk_process();
                    },
```

**File:** chain/src/verify.rs (L108-113)
```rust
                recv(self.truncate_block_rx) -> msg => match msg {
                    Ok(Request { responder, arguments: target_tip_hash }) => {
                        let _ = self.tx_pool_controller.suspend_chunk_process();
                        let _ = responder.send(self.processor.truncate(&target_tip_hash));
                        let _ = self.tx_pool_controller.continue_chunk_process();
                    },
```
