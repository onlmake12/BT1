Audit Report

## Title
Per-Transaction Cycle Budget Not Bounded by Remaining Block Cycles During Block Verification Enables CPU Exhaustion DoS — (File: `verification/contextual/src/contextual_block_verifier.rs`)

## Summary
In `BlockTxsVerifier::verify()`, every non-cached transaction in a block is individually verified against the full `max_block_cycles()` budget (3,500,000,000 cycles) rather than a proportional share. The aggregate block-level cycle check fires only after all transactions have completed execution in parallel via Rayon. A miner who crafts one valid PoW block containing many high-cycle transactions can force every receiving node to perform up to N × `max_block_cycles()` worth of CKB-VM execution before the block is ultimately rejected, causing sustained CPU exhaustion across the network.

## Finding Description
In `BlockTxsVerifier::verify()`, the resolved transactions are iterated in parallel via `par_iter()`. Each non-cached transaction is passed `self.context.consensus.max_block_cycles()` as its individual per-transaction cycle ceiling:

```rust
ContextualTransactionVerifier::new(...)
    .verify(
        self.context.consensus.max_block_cycles(),
        skip_script_verify,
    )
``` [1](#0-0) 

This value flows into `ContextualTransactionVerifier::verify()` at line 168 of `verification/src/transaction_verifier.rs`, which passes it directly to `self.script.verify(max_cycles)` — the CKB-VM script executor. The VM runs the script until it either completes or exhausts `max_block_cycles()` cycles. [2](#0-1) 

The aggregate sum is only checked at line 468, **after** all parallel workers have already finished:

```rust
if sum > self.context.consensus.max_block_cycles() {
    Err(BlockErrorKind::ExceededMaximumCycles.into())
``` [3](#0-2) 

The consensus constants confirm the scale of the amplification:
- `TWO_IN_TWO_OUT_CYCLES = 3_500_000`
- `TWO_IN_TWO_OUT_COUNT = 1_000`
- `MAX_BLOCK_CYCLES = 3_500_000 × 1_000 = 3_500_000_000`
- `MAX_BLOCK_BYTES = 597 × 1_000 = 597_000` [4](#0-3) 

The tx-pool path, by contrast, uses a configurable `max_tx_verify_cycles` per transaction (default: `TWO_IN_TWO_OUT_CYCLES × 20 = 70,000,000`), which is ~50× smaller than `max_block_cycles()`. [5](#0-4) [6](#0-5) 

The block verification path has no equivalent per-transaction guard. With `MAX_BLOCK_BYTES = 597,000` and minimal transactions sharing a cell dep (keeping per-tx byte cost low), a block can hold hundreds to thousands of transactions, each individually burning up to 3.5 billion cycles before the aggregate check fires.

## Impact Explanation
This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** Every full node that downloads and verifies the crafted block saturates its Rayon thread pool for the duration of the N × `max_block_cycles()` execution window, blocking all other block and transaction processing. Because the block is broadcast via standard P2P relay, the DoS is amplified across all peers simultaneously.

## Likelihood Explanation
The attacker must mine one valid PoW block — a one-time cost rather than an ongoing requirement. On testnet the cost is negligible. On mainnet the cost is non-trivial but bounded and finite; a motivated adversary seeking to disrupt a specific on-chain event may find the cost acceptable given the network-wide impact. No privileged access, leaked keys, or majority hashpower is required. The attack is externally reachable via the standard P2P block relay path and the `submit_block` RPC.

## Recommendation
Apply a per-transaction cycle cap during block verification that is proportional to the remaining block cycle budget, not the full `max_block_cycles()`. Because verification is currently parallel, a practical approach is to pass `max_block_cycles()` as the per-transaction limit but track a shared atomic counter of consumed cycles and abort early once the aggregate exceeds the budget. Alternatively, serialize verification and pass `remaining = max_block_cycles - cycles_consumed_so_far` to each successive `ContextualTransactionVerifier::verify()` call. Either approach bounds total verification work to at most `max_block_cycles()` regardless of transaction count, eliminating the N× amplification.

## Proof of Concept
1. Write a minimal RISC-V CKB script that executes a tight loop consuming exactly `max_block_cycles() - 1` cycles before returning success (exit code 0).
2. Deploy the script to a cell on testnet.
3. Build a block containing as many transactions as `MAX_BLOCK_BYTES` allows, each spending a cell locked by that script and referencing the script cell as a cell dep (shared, so byte cost per tx is minimal).
4. Mine a valid PoW nonce for the block header.
5. Relay the block to target nodes via P2P or `submit_block` RPC.
6. Each node's `BlockTxsVerifier::verify()` launches all transactions in parallel via `par_iter()`, each running the full loop up to `max_block_cycles()` cycles.
7. After all threads complete, the aggregate sum exceeds `max_block_cycles()` and the block is rejected at line 468 — but only after N × 3,500,000,000 cycles of CKB-VM work have been performed, exhausting CPU resources and blocking legitimate block processing.

### Citations

**File:** verification/contextual/src/contextual_block_verifier.rs (L403-435)
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

**File:** verification/src/transaction_verifier.rs (L162-169)
```rust
    pub fn verify(&self, max_cycles: Cycle, skip_script_verify: bool) -> Result<Completed, Error> {
        self.time_relative.verify()?;
        self.capacity.verify()?;
        let cycles = if skip_script_verify {
            0
        } else {
            self.script.verify(max_cycles)?
        };
```

**File:** spec/src/consensus.rs (L69-84)
```rust
/// cycles of a typical two-in-two-out tx.
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

**File:** tx-pool/src/util.rs (L85-110)
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
```

**File:** util/app-config/src/legacy/tx_pool.rs (L13-14)
```rust
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
```
