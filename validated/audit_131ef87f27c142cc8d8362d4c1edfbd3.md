All technical claims are verified against the actual code:

1. `check_tx_fee` at `tx-pool/src/util.rs` L42-45 uses `tx_size` only for the minimum fee check, with the comment explicitly acknowledging this limitation. [1](#0-0) 

2. In `_process_tx` (`tx-pool/src/process.rs`), `pre_check` (which calls `check_tx_fee`) runs at L715-717, `verify_rtx` runs at L724-732, and `TxEntry::new` is created at L751 with `verified.cycles` — with no post-verification weight-based fee check in between. [2](#0-1) 

3. `fee_rate()` in `entry.rs` L115-117 correctly uses `get_transaction_weight(self.size, self.cycles)` but is only used for eviction ordering, not admission gating. [3](#0-2) 

4. `limit_size` in `pool.rs` L298 evicts only on `total_tx_size > max_tx_pool_size` — never on cycle budget. [4](#0-3) 

5. `total_tx_cycles` is tracked in `pool_map.rs` L71 but confirmed never used as an eviction trigger anywhere. [5](#0-4) 

---

Audit Report

## Title
Tx-Pool Admission Uses Size-Only Fee Check, Allowing Cheap Cycle-Budget Exhaustion Griefing - (File: tx-pool/src/util.rs)

## Summary
`check_tx_fee` enforces the minimum fee rate using only the transaction's serialized byte size, while actual resource cost is measured by weight (`max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`). An attacker can craft transactions with small byte size (~200 bytes) but near-maximum cycle consumption (~70M cycles), paying only the size-based minimum fee (~200 shannons) while consuming cycle resources worth ~60× more in weight-equivalent terms. This enables cheap, sustained griefing of the tx-pool's cycle budget and node verification CPU.

## Finding Description
In `tx-pool/src/util.rs` (L42-45), `check_tx_fee` explicitly uses `tx_size` for the minimum fee calculation, with a comment acknowledging the limitation:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

This check runs during `pre_check` in `_process_tx` (`tx-pool/src/process.rs` L715-717), before `verify_rtx` executes scripts and measures actual cycles (L724-734). After verification, cycles are known but there is no post-verification weight-based fee re-check — the `TxEntry` is created directly with actual cycles at L751 and submitted.

The `fee_rate()` method on `TxEntry` (`tx-pool/src/component/entry.rs` L115-117) correctly uses weight:
```rust
let weight = get_transaction_weight(self.size, self.cycles);
FeeRate::calculate(self.fee, weight)
```
But this is only used for eviction ordering and block assembly sorting, not for admission gating.

Pool eviction (`limit_size` in `tx-pool/src/pool.rs` L298) triggers only on `total_tx_size > max_tx_pool_size` — never on cycle overflow. `total_tx_cycles` is tracked in `pool_map.rs` (L71) but is never used as an eviction trigger anywhere in the codebase.

A transaction with 200-byte size and 70M cycles passes admission at 200 shannons, but its actual `fee_rate()` is ~16.7 shannons/KW — 60× below the 1,000 shannons/KW minimum. The node must execute 70M cycles of script verification for each such transaction before it can be admitted or rejected.

## Impact Explanation
**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

Two concrete impacts:
1. **Verification CPU exhaustion**: Each attacker transaction forces the node to execute ~70M cycles of script verification before admission. At scale, this saturates verification worker threads, degrading processing of legitimate transactions.
2. **Pool churn and block assembly degradation**: Admitted attacker transactions have ~16.7 shannons/KW effective fee rate and are correctly deprioritized by the block assembler, but occupy pool byte-size space. When evicted as lowest fee-rate entries, the attacker resubmits, creating continuous pool churn and repeated verification overhead. The cost to fill the 180 MB pool is ~1.8 CKB (180 MB / 200 bytes × 200 shannons), 60× cheaper per weight-unit than legitimate transactions.

## Likelihood Explanation
- **Entry path**: Any unprivileged user via `send_transaction` JSON-RPC or P2P relay. No special role or key required.
- **Cost**: ~1.8 CKB to fill the pool with maximum-cycle transactions.
- **Persistence**: Attacker must continuously resubmit evicted transactions, but at 60× lower cost per weight-unit than legitimate transactions.
- **Naturally occurring**: High-cycle scripts (ZK verifiers, complex lock scripts) submitted with minimum fees can trigger this without malicious intent.
- **No special setup**: The high-cycle script can be stored in a cell dep (not in the transaction itself), keeping tx size minimal.

## Recommendation
After `verify_rtx` returns `verified.cycles`, perform a second weight-based fee check before creating the `TxEntry`:

```rust
let verified = try_or_return_with_snapshot!(verified_ret, snapshot);
// Post-verification weight-based fee check
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

The existing size-only check in `check_tx_fee` is retained as a fast pre-filter before script execution. The post-verification check closes the gap between admission cost and actual resource cost. Optionally, add a `total_tx_cycles` cap to `limit_size` to bound cycle accumulation independently of byte size.

## Proof of Concept
1. Deploy a CKB script that executes a tight loop consuming exactly 69,999,999 cycles. Store it in a live cell on-chain (cell dep, not in the transaction itself, keeping tx size ~200 bytes).
2. Craft a transaction: one input, one output (returning capacity minus fee), one cell dep referencing the high-cycle script. Fee = 200 shannons (`min_fee_rate × tx_size = 1000 × 200 / 1000`). Serialized size ≈ 200 bytes.
3. Submit via `send_transaction` RPC. Observe:
   - `check_tx_fee`: `min_fee = 1000 * 200 / 1000 = 200 shannons` ✓ passes
   - Script executes consuming ~70M cycles
   - `TxEntry` created with `cycles ≈ 70M`, `size ≈ 200`
   - Actual `fee_rate() = 200 * 1000 / max(200, 11940) ≈ 16.7 shannons/KW` — 60× below minimum
4. Repeat with ~900,000 such transactions (each spending a different UTXO, or chaining up to `max_ancestors_count = 25`).
5. Verify via `tx_pool_info` RPC: `total_tx_cycles` reaches an enormous value; block templates contain few or no attacker transactions (correctly deprioritized) but node verification threads are saturated processing the flood of 70M-cycle transactions.

### Citations

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

**File:** tx-pool/src/process.rs (L715-751)
```rust
        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);

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

        let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

        if let Some(declared) = declared_cycles
            && declared != verified.cycles
        {
            info!(
                "process_tx declared cycles not match verified cycles, declared: {}, verified: {}, tx_hash: {}",
                declared,
                verified.cycles,
                tx.hash()
            );
            return Some((
                Err(Reject::DeclaredWrongCycles(declared, verified.cycles)),
                snapshot,
            ));
        }

        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
```

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/pool.rs (L297-299)
```rust
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
```

**File:** tx-pool/src/component/pool_map.rs (L71-71)
```rust
    pub(crate) total_tx_cycles: Cycle,
```
