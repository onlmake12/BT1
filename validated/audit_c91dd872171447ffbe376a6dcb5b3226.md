Audit Report

## Title
Fee Gate Uses Serialized Size Only, Ignoring Cycle Consumption — Cycle-Heavy Transactions Admitted Below True Cost - (File: `tx-pool/src/util.rs`)

## Summary
The tx-pool admission check `check_tx_fee` computes the minimum required fee using only the transaction's serialized byte size, explicitly ignoring cycle consumption. Because CKB's true transaction weight is `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`, a transaction with a small serialized size but maximum cycle consumption passes the fee gate at a fraction of the fee its actual resource cost warrants. No second fee check is performed after `verify_rtx` returns the actual cycle count, allowing an attacker to flood the pool with cycle-heavy transactions at ~1.7% of the correct cost.

## Finding Description
`check_tx_fee` in `tx-pool/src/util.rs` (L28–54) computes the minimum fee using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

This is called from `pre_check` in `_process_tx` (`tx-pool/src/process.rs`, L715–717) before script execution. After `verify_rtx` returns the actual cycle count (L724–734), the code constructs a `TxEntry` at L751:

```rust
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
```

No second fee adequacy check against the cycle-adjusted weight is performed. The correct weight formula exists in `util/types/src/core/tx_pool.rs` (L298–303):

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

This function is used in `TxEntry::fee_rate()` (`tx-pool/src/component/entry.rs`, L114–118) for eviction ordering and fee estimation only — never for admission gating. The `declared_cycles` mismatch check (L736–748) only rejects remote transactions that lie about their cycle count; it does not enforce fee adequacy relative to actual cycles.

## Impact Explanation
A transaction with serialized size ~200 bytes and 70,000,000 actual cycles (the default `max_tx_verify_cycles = TWO_IN_TWO_OUT_CYCLES * 20`) passes the fee gate at 200 shannons (size-only check at 1000 shannons/KW), while the correct weight-based minimum fee is `max(200, 70_000_000 × 0.000_170_571_4) = 11,940 shannons` — approximately 60× underpriced. An attacker can repeatedly submit such transactions via the standard `send_transaction` RPC, saturating the verification worker pool and causing CKB network congestion with negligible cost. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation
The attack requires only: (1) a valid CKB transaction with a lock/type script executing a tight computation loop consuming near-maximum cycles, (2) sufficient CKB capacity to pay the underpriced fee, and (3) access to the standard `send_transaction` RPC endpoint or a peer relay connection. No privileged access, key leakage, or majority hashpower is required. The attack is repeatable as long as the attacker controls UTXOs, and each submission triggers a full VM execution at 60× below the weight-correct minimum fee.

## Recommendation
After `verify_rtx` returns the actual cycle count in `_process_tx`, perform a second fee adequacy check using the full weight:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(tx_pool_config.min_fee_rate, min_fee_by_weight.as_u64(), fee.as_u64())), snapshot));
}
```

The existing `check_tx_fee` can remain as the cheap pre-check to reject obviously underpaying transactions early, but a weight-accurate check must follow once cycles are known.

## Proof of Concept
1. Write a CKB script that executes a tight loop consuming ~70,000,000 cycles (near `DEFAULT_MAX_TX_VERIFY_CYCLES = TWO_IN_TWO_OUT_CYCLES * 20`).
2. Deploy it as a lock script on a cell with minimal capacity.
3. Craft a transaction spending that cell: serialized size ≈ 200 bytes.
4. Submit via `send_transaction` RPC with fee = 200 shannons (satisfies size-only check at 1000 shannons/KW, `min_fee = 1000 * 200 / 1000 = 200`).
5. Observe the transaction is accepted into the pool despite consuming 70M cycles of verification work (correct weight-based minimum would be 11,940 shannons).
6. Repeat with many UTXOs in parallel; each submission triggers a full 70M-cycle VM execution at a fee 60× below the weight-correct minimum.
7. The verification worker pool saturates; legitimate transactions queue indefinitely. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

**File:** tx-pool/src/process.rs (L715-717)
```rust
        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);
```

**File:** tx-pool/src/process.rs (L724-751)
```rust
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

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L14-14)
```rust
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
```
