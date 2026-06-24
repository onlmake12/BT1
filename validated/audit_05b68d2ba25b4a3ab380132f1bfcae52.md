Audit Report

## Title
Size-Only Minimum Fee Check Allows Cycles-Heavy Transactions to Underpay for Pool Admission - (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` gates pool admission using only the transaction's serialized byte size, while post-verification prioritization correctly uses the composite weight `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. An attacker can craft a ~100-byte transaction whose script consumes ~70M cycles, pay only the size-based minimum fee (~100 shannons), pass admission, trigger full script execution during `verify_rtx`, and permanently occupy a pool slot for 12 hours — all at ~119× below the correct resource cost. Repeated at scale, this saturates verification workers and pool capacity, causing network congestion.

## Finding Description

`check_tx_fee` explicitly uses only `tx_size` for the minimum fee computation:

```rust
// tx-pool/src/util.rs:42-45
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The developer comment acknowledges this is intentionally approximate, but no compensating composite check is performed after `verify_rtx`. In `_process_tx`, `pre_check` (which calls `check_tx_fee`) runs first, then `verify_rtx` executes the scripts:

```rust
// tx-pool/src/process.rs:715-734
let (ret, snapshot) = self.pre_check(&tx).await;
let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);
// ...
let verified_ret = verify_rtx(...).await;
let verified = try_or_return_with_snapshot!(verified_ret, snapshot);
// No composite fee check here — entry is submitted directly
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
``` [2](#0-1) 

After verification, `TxEntry::fee_rate()` correctly uses composite weight for prioritization:

```rust
// tx-pool/src/component/entry.rs:115-118
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [3](#0-2) 

The composite weight function: [4](#0-3) 

This creates a structural split: **admission uses size-only; prioritization uses composite weight**. A transaction admitted through the cheap size gate may have a composite fee rate far below `min_fee_rate`, meaning it will never be selected for block assembly but permanently occupies a pool slot and consumed verification cycles.

The default `max_tx_verify_cycles = TWO_IN_TWO_OUT_CYCLES * 20 ≈ 70,000,000` cycles: [5](#0-4) 

The default pool size limit is 180MB by byte size only, providing no protection against cycles-heavy small transactions: [6](#0-5) 

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

A ~100-byte transaction with a 70M-cycle script has composite weight `max(100, 70_000_000 × 0.000_170_571) = 11,940`. At `min_fee_rate = 1000 shannons/KW`:
- Attacker pays: `min_fee_rate.fee(100) = 100 shannons`
- Correct minimum fee: `min_fee_rate.fee(11,940) ≈ 11,940 shannons`
- Cost asymmetry: ~119×
- Admitted transaction's composite fee rate ≈ 8 shannons/KW — far below `min_fee_rate = 1000`
- Transaction is never mined; occupies pool slot for 12 hours (default `expiry_hours`)
- Each admission triggers full script execution consuming up to 70M cycles in verification workers
- At 180MB pool / ~100 bytes per tx: up to ~1.8M such transactions admitted, each burning 70M verification cycles

## Likelihood Explanation

The attack requires only an unprivileged `send_transaction` RPC call. No special role, key, or hashpower is needed — only enough CKB to pay the trivially small size-based fee and control over UTXOs locked by a cycles-heavy script. The discrepancy is structural and deterministic, independent of network conditions. The attack is repeatable as long as the attacker controls UTXOs, and the cost asymmetry is fixed at ~119× regardless of network state.

## Recommendation

Replace the size-only fee check in `check_tx_fee` with a composite weight check. Since cycles are unavailable before `verify_rtx`, two options exist:

1. **Deferred check (preferred):** Move the composite fee check to after `verify_rtx` completes, using actual measured cycles:
```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

2. **Conservative pre-check:** Keep the cheap pre-check but use `max_tx_verify_cycles` as a worst-case upper bound:
```rust
let worst_case_weight = get_transaction_weight(tx_size, max_tx_verify_cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(worst_case_weight);
```

## Proof of Concept

1. Deploy a lock script cell containing a tight RISC-V loop executing for ~70,000,000 cycles. The script code lives in a dep cell; the transaction itself is ~100 bytes serialized.
2. Craft a transaction spending a UTXO locked by this script. Set output capacity such that `fee = min_fee_rate.fee(100) = 100 shannons` (at default 1000 shannons/KW).
3. Submit via `send_transaction` RPC.
4. Node runs `check_tx_fee` with `tx_size = 100`, computes `min_fee = 100 shannons`, admits the transaction (fee ≥ min_fee).
5. Node executes the script for ~70M cycles during `verify_rtx`.
6. Transaction enters pool. Its `fee_rate()` = `FeeRate::calculate(100, 11940)` ≈ 8 shannons/KW — far below `min_fee_rate = 1000`.
7. Transaction is never selected for block assembly but occupies a pool slot until the 12-hour expiry.
8. Repeat with many UTXOs to exhaust pool capacity and verification worker threads at ~1/119th the legitimate cost.

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

**File:** tx-pool/src/process.rs (L715-753)
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
```

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
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

**File:** util/app-config/src/legacy/tx_pool.rs (L13-14)
```rust
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
```

**File:** util/app-config/src/legacy/tx_pool.rs (L19-20)
```rust
// Default max_tx_pool_size 180mb
const DEFAULT_MAX_TX_POOL_SIZE: usize = 180_000_000;
```
