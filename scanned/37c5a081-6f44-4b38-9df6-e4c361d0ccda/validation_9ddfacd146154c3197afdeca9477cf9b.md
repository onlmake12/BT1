Audit Report

## Title
Size-Only Minimum Fee Check Allows Cycles-Heavy Transactions to Underpay for Pool Admission - (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces `min_fee_rate` using only the transaction's serialized byte size, ignoring consumed cycles. Because CKB blocks are constrained by two independent limits (bytes and cycles), the correct resource unit is `get_transaction_weight(size, cycles) = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. Using size alone allows an attacker to craft transactions that are tiny in bytes but consume near-maximum cycles, passing the fee gate at a fraction of the cost that reflects their true block-resource footprint. These transactions are never mined (their composite fee rate is below `min_fee_rate`) but occupy pool slots and exhaust verification workers until the 12-hour expiry.

## Finding Description

`check_tx_fee` is called inside `pre_check` (before `verify_rtx`) and uses only `tx_size` to compute the minimum fee:

```rust
// tx-pool/src/util.rs:42-45
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The call ordering in `_process_tx` confirms that `pre_check` (which calls `check_tx_fee`) runs before `verify_rtx`, so cycles are not yet known at admission time: [2](#0-1) 

After verification, `TxEntry::fee_rate()` correctly uses the composite weight for prioritization:

```rust
// tx-pool/src/component/entry.rs:115-118
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [3](#0-2) 

The composite weight function is: [4](#0-3) 

This creates a structural split: **admission uses size-only; prioritization uses composite weight**. A transaction that passes the cheap size-based gate may have a composite fee rate far below `min_fee_rate`, meaning it will never be mined but still occupies pool space and consumes verification cycles.

The pool size limit is enforced by byte size only and does not compensate for this gap: [5](#0-4) 

The default `max_tx_verify_cycles`: [6](#0-5) 

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

A minimal transaction can be serialized in ~100 bytes. Its composite weight would be:
```
max(100, 70_000_000 × 0.000_170_571) = max(100, 11,940) = 11,940
```

The attacker pays `min_fee_rate.fee(100) = 100 shannons` but occupies `11,940` weight units of block resource — a ~119× undercharge ratio. At `min_fee_rate = 1000 shannons/KW`:
- Attacker pays: 100 shannons per transaction
- Correct minimum fee: 11,940 shannons
- Each transaction triggers full script execution (up to 70M cycles) during `verify_rtx`
- These transactions are never selected for block assembly (composite fee rate ~8 shannons/KW, far below `min_fee_rate = 1000`)
- They permanently occupy pool slots until the 12-hour expiry, displacing legitimate transactions
- Verification worker threads are saturated at 1/119th the legitimate cost

The pool's 180MB byte-size limit does not compensate: at ~100 bytes per transaction, up to ~1.8M such transactions could be admitted, each consuming 70M verification cycles. [7](#0-6) 

## Likelihood Explanation

The attack requires only an unprivileged RPC call to `send_transaction`. No special role, key, or hashpower is needed. The attacker needs only enough CKB to pay the size-based minimum fee (trivially small) and to control UTXOs. The discrepancy is structural and deterministic — it does not depend on network conditions. The attack is repeatable as long as the attacker controls UTXOs, and the cost asymmetry is fixed at ~119× regardless of network state.

## Recommendation

Replace the size-only fee check in `check_tx_fee` with the composite weight. Since cycles are not available before `verify_rtx`, two options exist:

1. **Deferred check (preferred):** Move the composite fee check to after `verify_rtx` completes, using the actual measured cycles:
```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

2. **Conservative pre-check:** Keep the cheap pre-check but use `max_tx_verify_cycles` as a worst-case upper bound for cycles to compute a worst-case weight, ensuring no cycles-heavy transaction can slip through at a size-only rate:
```rust
let worst_case_weight = get_transaction_weight(tx_size, max_tx_verify_cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(worst_case_weight);
```

## Proof of Concept

1. Deploy a lock script cell containing a tight RISC-V loop that executes for ~70,000,000 cycles. The script code lives in a separate cell (referenced as a dep); the transaction itself is ~100 bytes serialized.
2. Craft a transaction spending a UTXO locked by this script. Set output capacity such that `fee = min_fee_rate.fee(100) = 100 shannons` (at default 1000 shannons/KW).
3. Submit via `send_transaction` RPC.
4. The node runs `check_tx_fee` with `tx_size = 100`, computes `min_fee = 100 shannons`, and admits the transaction (fee ≥ min_fee).
5. The node then executes the script for ~70M cycles during `verify_rtx`.
6. The transaction enters the pool. Its `fee_rate()` = `FeeRate::calculate(100, 11940)` ≈ 8 shannons/KW — far below `min_fee_rate = 1000`.
7. The transaction is never selected for block assembly but occupies a pool slot until the 12-hour expiry.
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

**File:** tx-pool/src/process.rs (L715-734)
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

**File:** spec/src/consensus.rs (L83-84)
```rust
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```
