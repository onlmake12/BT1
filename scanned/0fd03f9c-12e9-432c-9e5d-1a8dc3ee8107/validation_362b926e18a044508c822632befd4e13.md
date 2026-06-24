Audit Report

## Title
Fee Rate Admission Check Uses `tx_size` Instead of Weight, Allowing High-Cycle Transactions to Bypass `min_fee_rate` - (File: tx-pool/src/util.rs)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using raw serialized byte count (`tx_size`) as the weight argument to `FeeRate::fee()`, even though `FeeRate` is defined as shannons per kilo-**weight** where weight = `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For transactions where script cycles dominate, the actual weight can be up to ~60× larger than `tx_size`. Because `check_tx_fee` is the only fee-rate admission gate and no second check is performed after `verify_rtx` determines actual cycles, any unprivileged RPC caller can submit transactions whose effective fee rate is far below the configured `min_fee_rate`.

## Finding Description

**Root cause — unit mismatch in `check_tx_fee`:**

`FeeRate` is defined as shannons per kilo-weight: [1](#0-0) 

The correct weight formula is: [2](#0-1) 

But `check_tx_fee` passes `tx_size` directly as the weight, with a comment explicitly acknowledging the theoretical incorrectness: [3](#0-2) 

**No second check after `verify_rtx`:**

In `_process_tx`, `pre_check` (which calls `check_tx_fee`) runs before script execution. After `verify_rtx` returns the actual cycles, no fee-rate re-check using the real weight is performed — the transaction proceeds directly to `submit_entry`: [4](#0-3) 

**Contrast with in-pool fee rate calculation:**

Once inside the pool, `TxEntry::fee_rate()` correctly uses `get_transaction_weight(self.size, self.cycles)`: [5](#0-4) 

This means the admission gate uses a weaker check than the pool's own internal fee-rate accounting — the discrepancy is structural.

**Concrete discrepancy:**

| Parameter | Value |
|---|---|
| `tx_size` | 200 bytes |
| `min_fee_rate` | 1,000 shannons/KW |
| `min_fee` (check uses) | `1000 × 200 / 1000 = 200 shannons` |
| Actual weight (max cycles 70M) | `max(200, 70_000_000 × 0.000_170_571_4) ≈ 11,940 bytes` |
| Effective fee rate | `200 × 1000 / 11940 ≈ 16.7 shannons/KW` |

The effective enforcement is ~60× weaker than the configured `min_fee_rate` for maximum-cycle transactions.

## Impact Explanation

This maps to the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**.

An attacker can fill the 180 MB tx-pool with computationally expensive, low-fee-rate transactions at negligible cost (~200 shannons per transaction at default parameters). Each submission forces `verify_rtx` to execute up to 70,000,000 script cycles of CPU work on the node. Across the network, nodes relay these transactions to each other, multiplying the CPU burden. Legitimate transactions are displaced or delayed, and block assemblers receive a polluted fee-rate ordering. The pool's eviction mechanism (`limit_size`) uses the correct weight-based fee rate and will eventually evict these transactions, but the attacker can continuously resubmit at trivial cost, sustaining the attack indefinitely.

## Likelihood Explanation

- **Entry path**: Any caller of `send_transaction` RPC — no privilege required.
- **Feasibility**: Crafting a transaction with a tight RISC-V computation loop consuming ~70M cycles and minimal inputs/outputs (small serialized size) is straightforward for any script author.
- **Cost**: Negligible — ~200 shannons per transaction at default `min_fee_rate = 1000`.
- **Persistence**: Transactions expire after `expiry_hours` (default 12 h), but resubmission cost remains trivial. The attacker can automate continuous resubmission.

## Recommendation

After `verify_rtx` returns actual cycles in `_process_tx`, perform a second fee-rate check using the real weight before calling `submit_entry`:

```rust
// After verify_rtx returns verified.cycles:
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_accurate = tx_pool_config.min_fee_rate.fee(actual_weight);
if fee < min_fee_accurate {
    return Some((Err(Reject::LowFeeRate(
        tx_pool_config.min_fee_rate,
        min_fee_accurate.as_u64(),
        fee.as_u64(),
    )), snapshot));
}
```

The existing `check_tx_fee` call in `pre_check` can remain as a cheap pre-filter to reject obviously underpriced transactions before incurring script execution cost.

## Proof of Concept

1. Craft a CKB transaction with:
   - A lock/type script that executes ~70,000,000 cycles (tight loop in a RISC-V binary).
   - Minimal inputs and outputs so `tx_size ≈ 200` bytes.
   - Fee = `min_fee_rate × tx_size / 1000 + 1` = 201 shannons (with default `min_fee_rate = 1000`).

2. Submit via `send_transaction` RPC.

3. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200 shannons`. Fee (201) ≥ min_fee (200) → **passes**. [6](#0-5) 

4. `verify_rtx` runs the script, consuming ~70,000,000 cycles. Actual weight = 11,940 bytes. Actual fee rate ≈ 16.8 shannons/KW — far below the 1,000 shannons/KW floor. [7](#0-6) 

5. Transaction enters the pool. Repeat to continuously fill the pool with ~60× underpriced, CPU-expensive transactions. [8](#0-7)

### Citations

**File:** util/types/src/core/fee_rate.rs (L3-5)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);
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

**File:** tx-pool/src/util.rs (L42-52)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
```

**File:** tx-pool/src/process.rs (L715-754)
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

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
        try_or_return_with_snapshot!(ret, submit_snapshot);
```

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```
