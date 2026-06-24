Audit Report

## Title
Per-Transaction Cycle Budget Not Bounded by Remaining Block Cycles During Block Verification Enables CPU Exhaustion DoS — (File: `verification/contextual/src/contextual_block_verifier.rs`)

## Summary
In `TransactionsVerifier::verify()`, every non-cached transaction in a block is individually verified against the full `max_block_cycles()` budget rather than a proportional share. The aggregate block-level cycle check fires only after all transactions have completed execution in parallel. A miner who crafts one valid PoW block containing many high-cycle transactions can force every receiving node to perform up to N × `max_block_cycles()` worth of CKB-VM execution before the block is ultimately rejected, causing sustained CPU exhaustion across the network.

## Finding Description
In `verification/contextual/src/contextual_block_verifier.rs`, `TransactionsVerifier::verify()` iterates all block transactions in parallel via `par_iter()`. Each non-cached transaction is passed `self.context.consensus.max_block_cycles()` as its individual per-transaction cycle ceiling at line 433:

```rust
.verify(
    self.context.consensus.max_block_cycles(),
    skip_script_verify,
)
```

This value flows directly into `ContextualTransactionVerifier::verify()` (`verification/src/transaction_verifier.rs`, line 168), which passes it to `self.script.verify(max_cycles)` — the CKB-VM script executor. The VM will run the script until it either completes or exhausts `max_block_cycles()` cycles.

The aggregate sum is only checked at line 468, after all parallel workers have already finished:

```rust
if sum > self.context.consensus.max_block_cycles() {
    Err(BlockErrorKind::ExceededMaximumCycles.into())
```

With `MAX_BLOCK_CYCLES = TWO_IN_TWO_OUT_CYCLES × TWO_IN_TWO_OUT_COUNT = 3,500,000 × 1,000 = 3,500,000,000` and `MAX_BLOCK_BYTES = 597,000`, a block can hold hundreds to thousands of minimal transactions (each referencing a shared looping script via cell deps, keeping per-tx byte cost low). Each such transaction can individually burn up to 3.5 billion cycles before the aggregate check fires, yielding total verification work of N × 3,500,000,000 cycles.

The tx-pool path, by contrast, uses a configurable `max_tx_verify_cycles` per transaction (`tx-pool/src/util.rs`, line 90), which is a smaller, operator-configurable cap. The block verification path has no equivalent guard.

## Impact Explanation
This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** Every full node that downloads and verifies the crafted block saturates its Rayon thread pool for the duration of the N × max_block_cycles() execution window, blocking all other block and transaction processing. Because the block is broadcast via standard P2P relay, the DoS is amplified across all peers simultaneously. Nodes may become unresponsive for an extended period depending on hardware and transaction count.

## Likelihood Explanation
The attacker must mine one valid PoW block — a one-time cost rather than an ongoing requirement. On testnet the cost is negligible. On mainnet the cost is non-trivial but bounded and finite; a motivated adversary (e.g., one seeking to disrupt a specific on-chain event or settlement window) may find the cost acceptable given the network-wide impact. No privileged access, leaked keys, or majority hashpower is required. The attack is externally reachable via the standard P2P block relay path and the `submit_block` RPC.

## Recommendation
Apply a per-transaction cycle cap during block verification that is proportional to the **remaining** block cycle budget, not the full `max_block_cycles()`. Because verification is currently parallel, a practical approach is to pass `max_block_cycles()` as the per-transaction limit but track a shared atomic counter of consumed cycles and abort early once the aggregate exceeds the budget. Alternatively, serialize verification and pass `remaining = max_block_cycles - cycles_consumed_so_far` to each successive `ContextualTransactionVerifier::verify()` call. Either approach bounds total verification work to at most `max_block_cycles()` regardless of transaction count, eliminating the N× amplification.

## Proof of Concept
1. Write a minimal RISC-V CKB script that executes a tight loop consuming exactly `max_block_cycles() - 1` cycles before returning success (exit code 0).
2. Deploy the script to a cell on testnet.
3. Build a block containing as many transactions as `MAX_BLOCK_BYTES` allows, each spending a cell locked by that script and referencing the script cell as a cell dep (shared, so byte cost per tx is minimal).
4. Mine a valid PoW nonce for the block header.
5. Relay the block to target nodes via P2P or `submit_block` RPC.
6. Each node's `TransactionsVerifier::verify()` launches all transactions in parallel via `par_iter()`, each running the full loop up to `max_block_cycles()` cycles.
7. After all threads complete, the aggregate sum exceeds `max_block_cycles()` and the block is rejected at line 468 — but only after N × 3,500,000,000 cycles of CKB-VM work have been performed, exhausting CPU resources and blocking legitimate block processing.