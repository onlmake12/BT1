### Title
Per-Transaction Cycle Budget Not Bounded by Remaining Block Cycles During Block Verification Enables CPU Exhaustion DoS — (File: `verification/contextual/src/contextual_block_verifier.rs`)

---

### Summary

In `TransactionsVerifier::verify()`, every transaction in a block is individually verified against the **full** `max_block_cycles()` budget. The aggregate block-level cycles check fires only after all transactions have already completed execution. A miner who crafts one valid PoW block containing many high-cycle transactions can force every receiving node to perform up to N × `max_block_cycles()` worth of CKB-VM execution before the block is ultimately rejected, causing sustained CPU exhaustion across the network.

---

### Finding Description

In `verification/contextual/src/contextual_block_verifier.rs`, the `TransactionsVerifier::verify()` function iterates all block transactions in parallel via `par_iter()`: [1](#0-0) 

Each non-cached transaction is passed `self.context.consensus.max_block_cycles()` as its individual per-transaction cycle ceiling: [2](#0-1) 

The aggregate sum is only compared against `max_block_cycles()` **after** all transactions have already finished executing: [3](#0-2) 

This means:
- Each of the N transactions in a block may individually consume up to `max_block_cycles()` cycles.
- Total verification work is bounded by **N × `max_block_cycles()`**, not `max_block_cycles()`.
- The block is rejected only after all that work is done.

The consensus constants make the amplification concrete: [4](#0-3) 

`MAX_BLOCK_CYCLES = 3,500,000 × 1,000 = 3,500,000,000` cycles. With `MAX_BLOCK_BYTES = 597,000` bytes, a block can hold hundreds of transactions. Each can individually burn the full 3.5 billion cycle budget before the aggregate check fires.

By contrast, the tx-pool path applies a configurable, smaller `max_tx_verify_cycles` per transaction: [5](#0-4) [6](#0-5) 

The block verification path has no equivalent per-transaction cap; it always uses the full block budget.

---

### Impact Explanation

An attacker who mines one valid PoW block containing many transactions — each carrying a script that loops to consume close to `max_block_cycles()` cycles — causes every node that downloads and verifies the block to execute up to N × 3,500,000,000 CKB-VM cycles before rejecting it. During this window the node's Rayon thread pool is saturated, blocking all other block and transaction processing. The block is broadcast to the entire network, so the DoS is amplified across all peers simultaneously. Nodes may be unresponsive for minutes to hours depending on hardware and the number of transactions packed into the block.

---

### Likelihood Explanation

The attacker must mine one valid PoW block, which is a one-time cost rather than an ongoing requirement. On testnet the cost is negligible. On mainnet the cost is non-trivial but the impact — stalling all full nodes — may justify it for a motivated adversary. No privileged access, leaked keys, or majority hashpower is required; a single block suffices. The attack is externally reachable via the standard P2P block relay path and the `submit_block` RPC.

---

### Recommendation

Apply a per-transaction cycle cap during block verification that is proportional to the **remaining** block cycle budget, not the full `max_block_cycles()`. Concretely, before verifying transaction `i`, compute `remaining = max_block_cycles - cycles_consumed_so_far` and pass `remaining` (or a configurable per-tx cap analogous to `max_tx_verify_cycles`) as the limit to `ContextualTransactionVerifier::verify()`. This bounds total verification work to at most `max_block_cycles()` regardless of how many transactions are in the block, eliminating the N× amplification.

---

### Proof of Concept

1. Craft a CKB script that executes a tight loop consuming exactly `max_block_cycles() - 1` cycles before returning success.
2. Build a block containing as many transactions as `MAX_BLOCK_BYTES` allows, each spending a cell locked by that script.
3. Mine a valid PoW nonce for the block header.
4. Relay the block to target nodes via the P2P network or `submit_block` RPC.
5. Each node's `TransactionsVerifier::verify()` launches all transactions in parallel via `par_iter()`, each running the full loop.
6. After all threads complete, the aggregate sum exceeds `max_block_cycles()` and the block is rejected at line 468 — but only after N × `max_block_cycles()` cycles of CKB-VM work have been performed, exhausting CPU resources and blocking legitimate block processing.

### Citations

**File:** verification/contextual/src/contextual_block_verifier.rs (L403-456)
```rust
        let ret = resolved
            .par_iter()
            .enumerate()
            .map(|(index, tx)| {
                let wtx_hash = tx.transaction.witness_hash();

                if let Some(completed) = fetched_cache.get(&wtx_hash) {
                    TimeRelativeTransactionVerifier::new(
                            Arc::clone(tx),
                            Arc::clone(&self.context.consensus),
                            self.context.store.as_data_loader(),
                            Arc::clone(&tx_env),
                        )
                        .verify()
                        .map_err(|error| {
                            BlockTransactionsError {
                                index: index as u32,
                                error,
                            }
                            .into()
                        })
                        .map(|_| (wtx_hash, *completed))
                } else {
                    ContextualTransactionVerifier::new(
                        Arc::clone(tx),
                        Arc::clone(&self.context.consensus),
                        self.context.store.as_data_loader(),
                        Arc::clone(&tx_env),
                    )
                    .verify(
                        self.context.consensus.max_block_cycles(),
                        skip_script_verify,
                    )
                    .map_err(|error| {
                        BlockTransactionsError {
                            index: index as u32,
                            error,
                        }
                        .into()
                    })
                    .map(|completed| (wtx_hash, completed))
                }.and_then(|result| {
                    if self.context.consensus.rfc0044_active(self.parent.epoch().number()) {
                        DaoScriptSizeVerifier::new(
                            Arc::clone(tx),
                            Arc::clone(&self.context.consensus),
                            self.context.store.as_data_loader(),
                        ).verify()?;
                    }
                    Ok(result)
                })
            })
            .skip(1) // skip cellbase tx
            .collect::<Result<Vec<(Byte32, Completed)>, Error>>()?;
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L458-472)
```rust
        let sum: Cycle = ret.iter().map(|(_, cache_entry)| cache_entry.cycles).sum();
        let cache_entires = ret
            .iter()
            .map(|(_, completed)| completed)
            .cloned()
            .collect();
        if !ret.is_empty() {
            self.update_cache(ret);
        }

        if sum > self.context.consensus.max_block_cycles() {
            Err(BlockErrorKind::ExceededMaximumCycles.into())
        } else {
            Ok((sum, cache_entires))
        }
```

**File:** spec/src/consensus.rs (L70-84)
```rust
pub const TWO_IN_TWO_OUT_CYCLES: Cycle = 3_500_000;
/// bytes of a typical two-in-two-out tx.
pub const TWO_IN_TWO_OUT_BYTES: u64 = 597;
/// count of two-in-two-out txs a block should capable to package.
const TWO_IN_TWO_OUT_COUNT: u64 = 1_000;
pub(crate) const DEFAULT_EPOCH_DURATION_TARGET: u64 = 4 * 60 * 60; // 4 hours, unit: second
const MILLISECONDS_IN_A_SECOND: u64 = 1000;
const MAX_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MIN_BLOCK_INTERVAL; // 1800
const MIN_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MAX_BLOCK_INTERVAL; // 300
pub(crate) const DEFAULT_PRIMARY_EPOCH_REWARD_HALVING_INTERVAL: EpochNumber =
    4 * 365 * 24 * 60 * 60 / DEFAULT_EPOCH_DURATION_TARGET; // every 4 years

/// The default maximum allowed size in bytes for a block
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```

**File:** tx-pool/src/process.rs (L719-732)
```rust
        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
        let tip_header = snapshot.tip_header();
        let tx_env = Arc::new(status.with_env(tip_header));

        let verified_ret = verify_rtx(
            Arc::clone(&snapshot),
            Arc::clone(&rtx),
            tx_env,
            &verify_cache,
            max_cycles,
            command_rx,
        )
        .await;
```

**File:** util/app-config/src/configs/tx_pool.rs (L20-22)
```rust
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
    /// max tx verify workers, default is 3/4 of cpu cores
```
