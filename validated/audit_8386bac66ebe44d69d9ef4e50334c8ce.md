Audit Report

## Title
Fee Rate Admission Check Uses Size-Only Weight While Actual Transaction Weight Is Cycles-Aware, Allowing Below-Minimum Fee Rate Transactions Into the Pool — (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only `tx_size` as the weight denominator, with an explicit code comment acknowledging this is a "cheap check." After `verify_rtx` returns the true cycle count in `_process_tx`, no fee rate re-validation occurs against the cycles-aware `get_transaction_weight` formula. This allows an attacker to admit transactions whose real fee rate is far below `min_fee_rate`, enabling mempool spam at drastically reduced cost.

## Finding Description

**Admission gate (`check_tx_fee`, `tx-pool/src/util.rs` L42–52):**

The code explicitly acknowledges the mismatch with a comment: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly."* The check computes `min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64)` and rejects only if `fee < min_fee`. Cycles are not yet known at this point.

**Actual weight model (`util/types/src/core/tx_pool.rs` L298–303):**

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

`DEFAULT_BYTES_PER_CYCLES` is calibrated so that `MAX_BLOCK_CYCLES * DEFAULT_BYTES_PER_CYCLES ≈ MAX_BLOCK_BYTES`. A cycles-saturating transaction therefore has the same weight as a bytes-saturating one.

**No re-check after verification (`tx-pool/src/process.rs` L715–754):**

`pre_check` (which calls `check_tx_fee`) runs before `verify_rtx`. After `verify_rtx` returns `verified.cycles`, the entry is created with the real cycle count and immediately submitted — no fee rate re-validation against `get_transaction_weight(tx_size, verified.cycles)` occurs:

```rust
let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);
// ... verify_rtx runs here, actual cycles now known ...
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
```

**Pool scoring uses the correct formula (`tx-pool/src/component/entry.rs` L115–118):**

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

This creates a structural incompatibility: the admission criterion is linear and size-only, while the rest of the system is cycles-aware, with no reconciliation point between them.

## Impact Explanation

An attacker crafts a transaction with minimal serialized size (e.g., 100 bytes) but high cycle consumption (e.g., 10,000,000 cycles). The admission check passes because `min_fee_rate * 100 / 1000` is paid. The actual weight is `max(100, 10,000,000 × 0.000_170_571_4) = 1,705`, so the real fee rate is approximately `min_fee_rate / 17`. At `max_block_cycles = 3,500,000,000`, the discount reaches ~1/5,900. This maps to **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**. The pool fills with below-minimum-fee-rate entries displacing legitimate transactions, and each admitted transaction still runs full script verification (`verify_rtx`) before the discrepancy is never caught, wasting CPU.

## Likelihood Explanation

The attack is reachable by any unprivileged actor via RPC (`send_transaction` / `test_tx_pool_accept`) or P2P relay (`RelayTransactions`). No authentication is required. Crafting a cycles-heavy, size-small transaction requires only writing a compact script that performs many arithmetic operations — straightforward for any script author with a valid UTXO to spend. The attack is repeatable and cheap.

## Recommendation

After `verify_rtx` returns the actual cycle count, re-validate the fee rate using the true weight before admitting the entry to the pool. In `_process_tx` (`tx-pool/src/process.rs`), after the `DeclaredWrongCycles` check (L749), add:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(
        tx_pool_config.min_fee_rate,
        min_fee_by_weight.as_u64(),
        fee.as_u64(),
    )), snapshot));
}
```

This closes the gap between the admission criterion and the actual weight model, consistent with how `TxEntry::fee_rate()` already computes the fee rate.

## Proof of Concept

1. Obtain a live UTXO with capacity `C` shannons.
2. Write a lock script that loops `N = 10,000,000` iterations of a simple arithmetic op, keeping the compiled binary small (< 200 bytes).
3. Construct a transaction spending that UTXO, with output capacity `C - fee` where `fee = min_fee_rate * serialized_size / 1000`.
4. Submit via `send_transaction` RPC.
5. Observe: the transaction is accepted. Query `get_transaction`, inspect `cycles`, compute `FeeRate::calculate(fee, get_transaction_weight(size, cycles))` — it will be far below `min_fee_rate`.
6. Repeat to fill the pool with below-minimum-fee-rate entries.

Root cause confirmed at:
- `tx-pool/src/util.rs` L42–52 (`check_tx_fee` using `tx_size` only) [1](#0-0) 
- `util/types/src/core/tx_pool.rs` L298–303 (`get_transaction_weight` using cycles) [2](#0-1) 
- `tx-pool/src/process.rs` L717–754 (no re-check after `verify_rtx`) [3](#0-2) 
- `tx-pool/src/component/entry.rs` L115–118 (`fee_rate()` correctly uses `get_transaction_weight`) [4](#0-3)

### Citations

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

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** tx-pool/src/process.rs (L717-754)
```rust
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
