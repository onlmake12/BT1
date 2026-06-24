Audit Report

## Title
Fee Rate Unit Mismatch in `check_tx_fee` Allows High-Cycle Transactions to Bypass Minimum Fee Rate Policy - (File: tx-pool/src/util.rs)

## Summary
`check_tx_fee` computes the minimum required fee using raw serialized `tx_size` as the weight argument to `FeeRate::fee()`, even though `FeeRate` is defined as shannons per kilo-weight and the actual transaction weight accounts for cycles via `get_transaction_weight`. After `verify_rtx` determines actual cycles, no second weight-based fee check is performed. Any unprivileged submitter can craft a high-cycle, small-size transaction and pay far less than the node operator's configured `min_fee_rate` policy requires.

## Finding Description
`FeeRate` is defined as shannons per kilo-weight: [1](#0-0) 

The actual transaction weight is: [2](#0-1) 

But `check_tx_fee` passes raw `tx_size` — not the actual weight — to `FeeRate::fee()`. The code comment explicitly acknowledges this is an approximation: [3](#0-2) 

In `_process_tx`, after `pre_check` (which calls `check_tx_fee` with size only), `verify_rtx` is called and returns `verified.cycles`. However, there is **no second fee rate check** using `get_transaction_weight(tx_size, verified.cycles)`. The entry is immediately constructed and submitted: [4](#0-3) 

The pool's eviction mechanism does use weight-based fee rate via `TxEntry::fee_rate()`: [5](#0-4) 

But eviction only occurs after admission and pool-full conditions — the CPU cost of script verification is already incurred by then.

## Impact Explanation
With default config (`min_fee_rate = 1000` shannons/KW, `max_tx_verify_cycles = 70,000,000`): a transaction with ~300 bytes serialized size but 70,000,000 cycles has actual weight ≈ 11,940. `check_tx_fee` requires only 300 shannons; the correct minimum is 11,940 shannons — a ~40× shortfall. An attacker can continuously submit such transactions, each consuming up to 70M CPU cycles during `verify_rtx`, at a fraction of the intended economic cost. This matches the allowed impact: **High — bad design which could cause CKB network congestion with few costs**.

## Likelihood Explanation
Any unprivileged caller of the `send_transaction` RPC can trigger this. For local submissions, `declared_cycles` is `None`, so `max_block_cycles()` is used as the cycle limit with no declared-cycle mismatch check: [6](#0-5) 

Crafting a valid transaction with a loop-heavy lock script consuming near-maximum cycles while keeping serialized size small is straightforward for any script author. No privileged access, leaked keys, or majority hashpower is required. The attack is repeatable.

## Recommendation
After `verify_rtx` returns `verified.cycles`, perform a second fee rate check using the actual transaction weight:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee {
    return Some((Err(Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64())), snapshot));
}
```

Insert this check in `_process_tx` between lines 734 and 751 of `tx-pool/src/process.rs`, after `verified.cycles` is known and before `TxEntry::new` is called. [7](#0-6) 

## Proof of Concept
1. Write a CKB lock script (RISC-V) that executes a tight loop consuming ~70,000,000 cycles. Compile and deploy it. The serialized transaction referencing this script is ~300 bytes.
2. Compute fee: `1000 × 300 / 1000 = 300 shannons`. Set transaction fee to exactly 300 shannons.
3. Submit via `send_transaction` RPC (local, no declared cycles).
4. `check_tx_fee` passes: `300 >= 300`. ✓
5. `verify_rtx` executes the script, consuming ~70,000,000 cycles.
6. No second fee check occurs. `TxEntry::new` is called with `verified.cycles = 70_000_000`.
7. Actual weight = `max(300, 11,940)` = 11,940. Effective fee rate ≈ 25 shannons/KW — ~40× below the configured 1,000 shannons/KW minimum.
8. Transaction is admitted. Repeat to flood the verify queue and pool with high-cycle transactions at a fraction of the intended minimum cost, exhausting node CPU. [8](#0-7) [9](#0-8)

### Citations

**File:** util/types/src/core/fee_rate.rs (L3-7)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);

const KW: u64 = 1000;
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

**File:** tx-pool/src/util.rs (L42-53)
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
    Ok(fee)
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
