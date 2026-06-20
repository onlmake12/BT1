### Title
Silently Discarded `Result` from `suspend_chunk_process` / `continue_chunk_process` During Block Verification — (`chain/src/verify.rs`)

### Summary

In `chain/src/verify.rs`, the return values of `tx_pool_controller.suspend_chunk_process()` and `tx_pool_controller.continue_chunk_process()` are explicitly discarded with `let _ =` at every call site inside the block-verification loop. If either call fails, the failure is invisible to the caller and execution continues as if the coordination succeeded. This is the direct Rust analog of the ERC20 unchecked-return-value class: a function that returns a `Result` signalling success or failure is called, the `Result` is thrown away, and the surrounding logic proceeds on a false assumption.

### Finding Description

`ConsumeUnverifiedBlocks::start()` is the main loop that dequeues and verifies every incoming block. Before processing each block it must pause the tx-pool's chunk processor (which runs long-running script verification for pending transactions) so that the two subsystems do not race over shared chain state. After the block is committed it must resume the chunk processor. [1](#0-0) [2](#0-1) [3](#0-2) 

All four call sites use `let _ = …;`, silently discarding the `Result`:

```rust
// line 81 – before processing an unverified block
let _ = self.tx_pool_controller.suspend_chunk_process();

// line 101 – after processing the block
let _ = self.tx_pool_controller.continue_chunk_process();

// lines 110-112 – truncate path
let _ = self.tx_pool_controller.suspend_chunk_process();
let _ = responder.send(self.processor.truncate(&target_tip_hash));
let _ = self.tx_pool_controller.continue_chunk_process();
```

`suspend_chunk_process` and `continue_chunk_process` communicate with the tx-pool service over a channel. They return `Result` because the channel send can fail (e.g., the tx-pool service has stopped, the channel is full, or the receiver has been dropped). [4](#0-3) 

There are two distinct failure modes, mirroring the two ERC20 impact types:

**Failure mode 1 — `suspend_chunk_process` fails silently.**
The tx-pool chunk processor keeps running while the chain is being reorganised. The chunk processor resolves cell inputs against the live-cell set. During a reorg, cells that are being detached or attached are in a transient state. A chunk processor running concurrently can resolve inputs against a cell set that is partially updated, producing incorrect script-verification outcomes (accepting a transaction whose inputs are being spent by the reorg block, or rejecting one whose inputs are being restored). Those outcomes are cached in `txs_verify_cache` and reused by subsequent block assembly. [5](#0-4) 

**Failure mode 2 — `suspend_chunk_process` succeeds but `continue_chunk_process` fails silently.**
The chunk processor is left permanently suspended. No further pending transaction can complete script verification via the chunk path. The tx-pool stops producing valid block templates for miners, constituting a persistent denial-of-service against block assembly without any observable error. [6](#0-5) 

### Impact Explanation

- **Incorrect cached verification results**: stale or wrong `Completed` cache entries produced during a concurrent chunk run are consumed by `reconcile_main_chain` and stored in `BlockExt.cycles` / `txs_fees`, corrupting fee accounting and cycle reporting for the committed block. [7](#0-6) 
- **Incorrect block templates**: a miner calling `get_block_template` may receive a template containing transactions whose cached verification result was computed against inconsistent chain state, leading to a block that fails contextual verification when relayed.
- **Permanent tx-pool stall** (failure mode 2): chunk-dependent transactions are never resolved; the node silently stops being able to assemble blocks containing complex scripts.

### Likelihood Explanation

The tx-pool service runs in a separate async task. Under high load the internal channel (`TxPoolController` → service) can be full; if the service task has panicked or been stopped by the stop-handler, every send returns an error. Both conditions are reachable without any privileged access: a block relayer that continuously sends valid blocks at high rate can saturate the channel, and any internal panic in the tx-pool service (e.g., triggered by a crafted transaction) leaves the channel permanently closed. Every subsequent block processed by `ConsumeUnverifiedBlocks` will silently skip the suspension step.

### Recommendation

Replace every `let _ = self.tx_pool_controller.suspend_chunk_process();` and `let _ = self.tx_pool_controller.continue_chunk_process();` with proper error handling. At minimum, log and propagate the error so that block processing is aborted rather than continuing with a false assumption:

```rust
if let Err(e) = self.tx_pool_controller.suspend_chunk_process() {
    error!("failed to suspend chunk process before block verification: {}", e);
    // abort or retry rather than proceeding
    return;
}
```

The same applies to the `continue_chunk_process` call: if resumption fails, the node must surface the error (e.g., trigger a controlled shutdown) rather than leaving the chunk processor permanently suspended.

### Proof of Concept

1. Start a CKB node with a tx-pool containing a pending transaction that requires chunk-based script verification (any transaction with a non-trivial lock script).
2. Arrange for the tx-pool service channel to be at capacity (e.g., by flooding the service with RPC calls) at the moment a new block arrives from a peer.
3. `ConsumeUnverifiedBlocks::start()` calls `suspend_chunk_process()`; the channel send fails; `let _ =` discards the error.
4. The chunk processor continues running against the pre-reorg cell set while `reconcile_main_chain` modifies the live-cell index.
5. The chunk processor caches a verification result computed against inconsistent state.
6. The next `get_block_template` RPC call returns a template that includes the transaction with the corrupted cached result, producing a block that will be rejected by peers. [8](#0-7) [9](#0-8)

### Citations

**File:** chain/src/verify.rs (L71-126)
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
                    Err(err) => {
                        error!("unverified_block_rx err: {}", err);
                        return;
                    },
                },
                recv(self.truncate_block_rx) -> msg => match msg {
                    Ok(Request { responder, arguments: target_tip_hash }) => {
                        let _ = self.tx_pool_controller.suspend_chunk_process();
                        let _ = responder.send(self.processor.truncate(&target_tip_hash));
                        let _ = self.tx_pool_controller.continue_chunk_process();
                    },
                    Err(err) => {
                        info!("truncate_block_tx has been closed, err: {}", err);
                        return;
                    },
                },
                recv(self.stop_rx) -> _ => {
                    info!("verify_blocks thread received exit signal, exit now");
                    break;
                }

            }
        }
    }
```

**File:** chain/src/verify.rs (L620-621)
```rust
        let txs_verify_cache = self.shared.txs_verify_cache();
        let async_handle = self.shared.tx_pool_controller().handle();
```

**File:** chain/src/verify.rs (L674-692)
```rust
                                Ok((cycles, cache_entries)) => {
                                    let txs_sizes = resolved
                                        .iter()
                                        .map(|rtx| {
                                            rtx.transaction.data().serialized_size_in_block() as u64
                                        })
                                        .collect();
                                    txn.attach_block(b)?;
                                    attach_block_cell(&txn, b)?;
                                    mmr.push(b.digest())
                                        .map_err(|e| InternalErrorKind::MMR.other(e))?;

                                    self.insert_ok_ext(
                                        &txn,
                                        &b.header().hash(),
                                        ext.clone(),
                                        Some(&cache_entries),
                                        Some(txs_sizes),
                                    )?;
```
