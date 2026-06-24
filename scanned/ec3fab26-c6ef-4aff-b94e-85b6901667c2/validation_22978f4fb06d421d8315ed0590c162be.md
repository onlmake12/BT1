The code behavior described in the claim is confirmed. Let me verify the key facts before rendering a verdict.

**Confirmed facts from the code:**

1. Each non-cached transaction is passed `self.context.consensus.max_block_cycles()` as its individual cycle ceiling at line 433. [1](#0-0) 

2. The aggregate sum check fires only after all transactions have completed execution at line 468. [2](#0-1) 

3. `MAX_BLOCK_CYCLES = TWO_IN_TWO_OUT_CYCLES × TWO_IN_TWO_OUT_COUNT = 3,500,000 × 1,000 = 3,500,000,000`. [3](#0-2) 

4. The tx-pool path uses a configurable `max_tx_verify_cycles` (default `TWO_IN_TWO_OUT_CYCLES × 20 = 70,000,000`) as the per-transaction cap, not `max_block_cycles()`. [4](#0-3) [5](#0-4) 

5. The `par_iter()` with `.collect::<Result<...>>()?` does not short-circuit on accumulated cycle budget — it only stops on a per-transaction error. All N transactions run to completion before the aggregate check. [6](#0-5) 

There is no intermediate cycle accumulation guard, no remaining-budget cap passed to each transaction, and no early exit once the running total would exceed `max_block_cycles()`.

---

Audit Report

## Title
Per-Transaction Cycle Budget Not Bounded by Remaining Block Cycles During Block Verification Enables CPU Exhaustion DoS — (File: `verification/contextual/src/contextual_block_verifier.rs`)

## Summary
In `TransactionsVerifier::verify()`, every non-cached transaction in a block is individually verified against the full `max_block_cycles()` budget (3,500,000,000 cycles). The aggregate block-level cycle check fires only after all transactions have already completed execution. A miner who crafts one valid PoW block containing many high-cycle transactions can force every receiving node to perform up to N × `max_block_cycles()` worth of CKB-VM execution before the block is ultimately rejected, causing sustained CPU exhaustion across the network.

## Finding Description
In `TransactionsVerifier::verify()` (`verification/contextual/src/contextual_block_verifier.rs`, L387–473), the resolved transactions are iterated in parallel via `par_iter()`. For each non-cached transaction, `ContextualTransactionVerifier::verify()` is called with `self.context.consensus.max_block_cycles()` as the cycle ceiling (L432–435). This is the full block budget of 3,500,000,000 cycles, not a per-transaction fraction of it.

The `.collect::<Result<Vec<(Byte32, Completed)>, Error>>()?` at L456 gathers all results. Because all transactions in the attack scenario succeed individually (each consuming just under `max_block_cycles()` cycles), there is no early termination. Only after all N threads complete does the code sum the cycles at L458 and compare against `max_block_cycles()` at L468, rejecting the block with `ExceededMaximumCycles`.

The existing per-transaction guard is `max_block_cycles()` itself — the same value used for the aggregate check — so it provides no meaningful per-transaction protection when multiple transactions are present. The tx-pool path, by contrast, uses a configurable `max_tx_verify_cycles` (default `TWO_IN_TWO_OUT_CYCLES × 20 = 70,000,000` cycles, `tx-pool/src/util.rs` L90) as the per-transaction cap, which is 50× smaller than `max_block_cycles()`.

With `MAX_BLOCK_BYTES = 597,000` bytes and a minimal transaction size of ~100–200 bytes, a block can contain hundreds to thousands of transactions. Each can individually burn up to 3,500,000,000 cycles before the aggregate check fires, yielding total verification work of N × 3,500,000,000 cycles per block received.

## Impact Explanation
This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node** and **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

A single crafted block causes every full node that downloads it to saturate its Rayon thread pool with CKB-VM execution for an extended period, blocking all other block and transaction processing. Because the block is broadcast via standard P2P relay, the DoS is amplified across all peers simultaneously. Nodes may become unresponsive for minutes to hours depending on hardware and transaction count, effectively stalling the network.

## Likelihood Explanation
The attacker must mine one valid PoW block. On testnet the cost is negligible. On mainnet the cost is non-trivial but is a one-time expenditure, not an ongoing requirement. No privileged access, leaked keys, or majority hashpower is required. The attack is externally reachable via the standard P2P block relay path and the `submit_block` RPC. The block need not be accepted by the network to cause the DoS — the damage occurs during verification before rejection.

## Recommendation
Pass a per-transaction cycle cap proportional to the **remaining** block cycle budget rather than the full `max_block_cycles()`. Because the parallel iterator makes a running total impractical, the simplest correct bound is `max_block_cycles() / number_of_non_cellbase_transactions` computed before the `par_iter()` loop, or alternatively a configurable per-transaction cap analogous to `max_tx_verify_cycles` applied during block verification. This bounds total verification work to at most `max_block_cycles()` regardless of transaction count, eliminating the N× amplification.

## Proof of Concept
1. Deploy a cell containing a CKB script that executes a tight loop consuming exactly `max_block_cycles() - 1` cycles before returning exit code 0.
2. Create a set of transactions, each spending a cell locked by that script. Size the set to fill `MAX_BLOCK_BYTES` (e.g., ~1,000 minimal transactions at ~597 bytes each).
3. Mine a valid PoW nonce for the block header containing all these transactions.
4. Relay the block to target nodes via P2P or `submit_block` RPC.
5. Each node's `TransactionsVerifier::verify()` launches all transactions in parallel via `par_iter()`, each running the full loop to completion.
6. After all threads complete, the aggregate sum at L458 exceeds `max_block_cycles()` and the block is rejected at L468–469 — but only after N × 3,500,000,000 cycles of CKB-VM work have been performed, exhausting CPU resources and blocking legitimate block processing.

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

**File:** util/app-config/src/legacy/tx_pool.rs (L14-14)
```rust
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
```

**File:** tx-pool/src/util.rs (L85-131)
```rust
pub(crate) async fn verify_rtx(
    snapshot: Arc<Snapshot>,
    rtx: Arc<ResolvedTransaction>,
    tx_env: Arc<TxVerifyEnv>,
    cache_entry: &Option<CacheEntry>,
    max_tx_verify_cycles: Cycle,
    command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
) -> Result<Completed, Reject> {
    let consensus = snapshot.cloned_consensus();
    let data_loader = snapshot.as_data_loader();

    if let Some(completed) = cache_entry {
        TimeRelativeTransactionVerifier::new(rtx, consensus, data_loader, tx_env)
            .verify()
            .map(|_| *completed)
            .map_err(Reject::Verification)
    } else if let Some(command_rx) = command_rx {
        ContextualTransactionVerifier::new(
            Arc::clone(&rtx),
            consensus,
            data_loader,
            Arc::clone(&tx_env),
        )
        .verify_with_pause(max_tx_verify_cycles, command_rx)
        .await
        .and_then(|result| {
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
        .map_err(Reject::Verification)
    } else {
        block_in_place(|| {
            ContextualTransactionVerifier::new(Arc::clone(&rtx), consensus, data_loader, tx_env)
                .verify(max_tx_verify_cycles, false)
                .and_then(|result| {
                    DaoScriptSizeVerifier::new(
                        rtx,
                        snapshot.cloned_consensus(),
                        snapshot.as_data_loader(),
                    )
                    .verify()?;
                    Ok(result)
                })
                .map_err(Reject::Verification)
        })
    }
```
