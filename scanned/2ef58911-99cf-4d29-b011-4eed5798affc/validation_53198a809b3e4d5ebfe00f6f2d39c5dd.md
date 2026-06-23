### Title
Per-Transaction Cycle Budget Not Reduced by Prior Transactions in Block Verification — (`verification/contextual/src/contextual_block_verifier.rs`)

---

### Summary

In `BlockTxsVerifier::verify`, every transaction in a block is independently given the **full** `max_block_cycles()` budget. The aggregate cycle sum is only checked *after* all transactions have been executed. A malicious miner can craft a block whose transactions each consume nearly `max_block_cycles` cycles, forcing every honest verifying node to expend up to `N × max_block_cycles` CPU cycles before the block is ultimately rejected — a direct computational DoS on block verification.

---

### Finding Description

`BlockTxsVerifier::verify` iterates over all resolved transactions in a block using Rayon's `par_iter()`. For each transaction that is not in the verification cache, it calls `ContextualTransactionVerifier::verify` with the **full** `max_block_cycles()` as the cycle cap:

```rust
// verification/contextual/src/contextual_block_verifier.rs:432-434
.verify(
    self.context.consensus.max_block_cycles(),
    skip_script_verify,
)
```

After all transactions complete, the sum of their individual cycle counts is compared against `max_block_cycles()`:

```rust
// line 458, 468-469
let sum: Cycle = ret.iter().map(|(_, cache_entry)| cache_entry.cycles).sum();
if sum > self.context.consensus.max_block_cycles() {
    Err(BlockErrorKind::ExceededMaximumCycles.into())
```

There is no progressive reduction of the remaining cycle budget across transactions. Each transaction is independently permitted to run for up to `max_block_cycles` cycles, regardless of how many cycles prior transactions already consumed.

This mirrors the external report exactly: in that report, each NFT creator recipient received the full `SEND_VALUE_GAS_LIMIT_MULTIPLE_RECIPIENTS` gas allowance rather than a decremented remainder, allowing malicious recipients to collectively consume far more gas than the intended single-block budget. Here, each transaction receives the full block cycle budget rather than the remaining budget, allowing a malicious miner to force verifiers to execute far more cycles than the block limit permits.

---

### Impact Explanation

`max_block_cycles = TWO_IN_TWO_OUT_CYCLES × 1000 = 3,500,000,000` cycles.

A block can contain up to approximately 1,000 transactions (bounded by `max_block_bytes = 597,000` bytes). A malicious miner crafts each transaction to contain a script that loops for just under `max_block_cycles` cycles. Each transaction individually passes the per-transaction cycle check (it does not exceed `max_block_cycles`), but the aggregate sum far exceeds the block limit, so the block is ultimately rejected.

However, before rejection, every honest node has already executed up to `1,000 × 3,500,000,000 = 3.5 × 10¹²` total CPU cycles across all parallel workers. Even with Rayon parallelism across 8 threads, the wall-clock cost per malicious block is on the order of minutes of sustained CPU saturation per node. An attacker who can produce valid PoW blocks (even at low hashrate) can repeatedly submit such blocks to cause sustained computational DoS on the network's verifying nodes.

---

### Likelihood Explanation

Mining in CKB is permissionless. Any participant with even a small fraction of hashpower will occasionally produce a valid block. The attacker does not need a majority of hashpower — a single valid block is sufficient to trigger the expensive verification on all connected peers. The scripts needed to consume near-`max_block_cycles` cycles are trivially constructable (a simple RISC-V loop). The attack is repeatable as long as the attacker can find valid PoW solutions.

---

### Recommendation

Pass the **remaining** cycle budget to each transaction verifier, decrementing it by the cycles consumed by each preceding transaction, analogous to how `TransactionScriptsVerifier::verify` correctly passes `max_cycles - cycles` to each script group:

```rust
// script/src/verify.rs:203-204 — correct pattern
let used_cycles = self
    .verify_script_group(group, max_cycles - cycles)
```

In `BlockTxsVerifier::verify`, maintain a running `remaining_cycles` counter and pass it to each `ContextualTransactionVerifier::verify` call instead of the full `max_block_cycles()`. Because the transactions are currently verified via `par_iter()`, this requires either switching to sequential iteration or pre-computing a per-transaction cycle cap (e.g., `max_block_cycles / tx_count`) before the parallel phase.

---

### Proof of Concept

**Root cause — full budget given to every transaction:** [1](#0-0) 

**Aggregate check only after all transactions complete:** [2](#0-1) 

**Correct pattern (remaining budget decremented per group) in script verifier:** [3](#0-2) 

**`max_block_cycles` constant — 3.5 billion cycles:** [4](#0-3) 

**Block size bound (~1,000 transactions):** [5](#0-4)

### Citations

**File:** verification/contextual/src/contextual_block_verifier.rs (L426-435)
```rust
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

**File:** verification/contextual/src/contextual_block_verifier.rs (L458-469)
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
```

**File:** script/src/verify.rs (L200-213)
```rust
        // Now run each script group
        for (_hash, group) in self.groups() {
            // max_cycles must reduce by each group exec
            let used_cycles = self
                .verify_script_group(group, max_cycles - cycles)
                .map_err(|e| {
                    #[cfg(feature = "logging")]
                    logging::on_script_error(_hash, &self.hash(), &e);
                    e.source(group)
                })?;

            cycles = wrapping_cycles_add(cycles, used_cycles, group)?;
        }
        Ok(cycles)
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
