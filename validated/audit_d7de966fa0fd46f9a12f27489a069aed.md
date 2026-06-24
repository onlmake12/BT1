All five factual claims in the report have been verified against the actual source code:

1. **Line 433** confirmed: `self.context.consensus.max_block_cycles()` is passed as the per-transaction cycle ceiling for every non-cached transaction. [1](#0-0) 

2. **Line 468** confirmed: aggregate sum check fires only after `.collect()` at line 456 has gathered all results. [2](#0-1) 

3. **`MAX_BLOCK_CYCLES`** confirmed: `3_500_000 × 1_000 = 3_500_000_000`. [3](#0-2) 

4. **`DEFAULT_MAX_TX_VERIFY_CYCLES`** confirmed: `TWO_IN_TWO_OUT_CYCLES * 20 = 70_000_000`. [4](#0-3) 

5. **tx-pool path** confirmed: uses `max_tx_verify_cycles` (not `max_block_cycles()`) as the per-transaction cap. [5](#0-4) 

The `par_iter().collect::<Result<...>>()` short-circuits only on `Err`. In the attack scenario every transaction returns `Ok` (each individually stays under `max_block_cycles()`), so no short-circuit occurs and all N threads run to completion before the aggregate check at line 468 rejects the block.

---

Audit Report

## Title
Per-Transaction Cycle Budget Not Bounded by Remaining Block Cycles During Block Verification Enables CPU Exhaustion DoS — (File: `verification/contextual/src/contextual_block_verifier.rs`)

## Summary
In `BlockTxsVerifier::verify()`, every non-cached transaction in a block is individually verified against the full `max_block_cycles()` budget (3,500,000,000 cycles). The aggregate block-level cycle check fires only after all transactions have already completed execution. A miner who crafts one valid PoW block containing many high-cycle transactions can force every receiving node to perform up to N × `max_block_cycles()` worth of CKB-VM execution before the block is ultimately rejected, causing sustained CPU exhaustion across the network.

## Finding Description
In `BlockTxsVerifier::verify()` (`verification/contextual/src/contextual_block_verifier.rs`, L387–473), resolved transactions are iterated in parallel via `par_iter()`. For each non-cached transaction, `ContextualTransactionVerifier::verify()` is called with `self.context.consensus.max_block_cycles()` as the cycle ceiling (L432–435) — the full block budget of 3,500,000,000 cycles, not a per-transaction fraction of it.

The `.collect::<Result<Vec<(Byte32, Completed)>, Error>>()?` at L456 gathers all results. Because all transactions in the attack scenario succeed individually (each consuming just under `max_block_cycles()` cycles and returning `Ok`), Rayon's short-circuit-on-error mechanism never fires. Only after all N threads complete does the code sum cycles at L458 and compare against `max_block_cycles()` at L468, rejecting the block with `ExceededMaximumCycles`.

The existing per-transaction guard is `max_block_cycles()` itself — the same value used for the aggregate check — so it provides no meaningful per-transaction protection when multiple transactions are present. The tx-pool path, by contrast, uses a configurable `max_tx_verify_cycles` (default `TWO_IN_TWO_OUT_CYCLES × 20 = 70,000,000` cycles, `util/app-config/src/legacy/tx_pool.rs` L14) as the per-transaction cap, which is 50× smaller than `max_block_cycles()`.

With `MAX_BLOCK_BYTES = 597,000` bytes and CKB's cell-reference model (script code lives in a referenced cell, not in the transaction itself), a block can contain ~1,000 minimal transactions each referencing the same high-cycle script cell. Each transaction individually burns up to 3,500,000,000 cycles before the aggregate check fires, yielding total verification work of N × 3,500,000,000 cycles per block received.

## Impact Explanation
This matches two allowed High-severity impacts: **Vulnerabilities which could easily crash a CKB node** and **Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. A single crafted block causes every full node that downloads it to saturate its Rayon thread pool with CKB-VM execution for an extended period, blocking all other block and transaction processing. Because the block is broadcast via standard P2P relay, the DoS is amplified across all peers simultaneously.

## Likelihood Explanation
The attacker must mine one valid PoW block. On testnet the cost is negligible. On mainnet the cost is non-trivial but is a one-time expenditure. No privileged access, leaked keys, or majority hashpower is required. The attack is externally reachable via the standard P2P block relay path and the `submit_block` RPC. The block need not be accepted by the network to cause the DoS — the damage occurs during verification before rejection.

## Recommendation
Pass a per-transaction cycle cap proportional to the remaining block cycle budget rather than the full `max_block_cycles()`. Because the parallel iterator makes a running total impractical, the simplest correct bound is `max_block_cycles() / number_of_non_cellbase_transactions` computed before the `par_iter()` loop, or alternatively a configurable per-transaction cap analogous to `max_tx_verify_cycles` applied during block verification. This bounds total verification work to at most `max_block_cycles()` regardless of transaction count, eliminating the N× amplification.

## Proof of Concept
1. Deploy a cell containing a CKB script that executes a tight loop consuming exactly `max_block_cycles() - 1` (3,499,999,999) cycles before returning exit code 0.
2. Create ~1,000 transactions, each spending a cell locked by that script. Because the script code lives in a referenced cell, each transaction remains small (~597 bytes), fitting within `MAX_BLOCK_BYTES = 597,000`.
3. Mine a valid PoW nonce for the block header containing all these transactions.
4. Relay the block to target nodes via P2P or `submit_block` RPC.
5. Each node's `BlockTxsVerifier::verify()` launches all transactions in parallel via `par_iter()`, each running the full loop to completion (each returns `Ok` since each is individually under `max_block_cycles()`).
6. After all threads complete, the aggregate sum at L458 exceeds `max_block_cycles()` and the block is rejected at L468–469 — but only after ~1,000 × 3,500,000,000 = ~3.5 × 10¹² cycles of CKB-VM work have been performed, exhausting CPU resources and blocking legitimate block processing.

### Citations

**File:** verification/contextual/src/contextual_block_verifier.rs (L432-435)
```rust
                    .verify(
                        self.context.consensus.max_block_cycles(),
                        skip_script_verify,
                    )
```

**File:** verification/contextual/src/contextual_block_verifier.rs (L456-469)
```rust
            .collect::<Result<Vec<(Byte32, Completed)>, Error>>()?;

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

**File:** tx-pool/src/util.rs (L108-119)
```rust
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
```
