Audit Report

## Title
Tx-Pool Min-Fee-Rate Admission Check Uses Only Serialized Size, Enabling Cheap CPU-Exhaustion DoS via Cycle-Heavy Transactions — (`tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size, while the actual block resource consumed is `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. An unprivileged attacker can craft a small-serialized-size, maximum-cycle transaction that passes the admission gate with a fee ~40× below the effective minimum, then flood the verify queue — whose capacity is also measured in serialized bytes — forcing nodes to execute up to 70M cycles of script verification per transaction at negligible cost.

## Finding Description

**Root cause — size-only admission check:**

In `tx-pool/src/util.rs` lines 42–52, `check_tx_fee` computes the minimum required fee using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { return Err(reject); }
``` [1](#0-0) 

The actual weight used for pool sorting, eviction, and block assembly is defined in `util/types/src/core/tx_pool.rs`:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
``` [2](#0-1) 

`TxEntry::fee_rate()` uses this correct weight-based calculation: [3](#0-2) 

**No second check after verification:**

In `_process_tx` (`tx-pool/src/process.rs`), `pre_check` (which calls `check_tx_fee`) runs before `verify_rtx`. After `verify_rtx` returns the actual `verified.cycles`, a `TxEntry` is created and submitted with no subsequent fee-rate check against the real weight: [4](#0-3) 

**Verify queue capacity measured in serialized bytes:**

The verify queue enforces a 256MB limit (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`) based on serialized transaction size, not weight: [5](#0-4) [6](#0-5) 

A 300-byte transaction contributes only 300 bytes to this limit, allowing ~853,000 such transactions to fill the queue simultaneously — each requiring 70M cycles of script execution.

**Pool eviction also measured in serialized bytes:**

`limit_size` evicts based on `total_tx_size > max_tx_pool_size`, where `total_tx_size` is serialized bytes. A 300-byte, 70M-cycle transaction contributes only 300 bytes to the 180MB pool limit. [7](#0-6) 

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker submitting 300-byte / 70M-cycle transactions at 300 shannons each can:
- Fill the 256MB verify queue with ~853,000 transactions (256MB / 300 bytes)
- Force nodes to execute ~59.7 trillion cycles of script verification (853,000 × 70M)
- Do so at a fee rate of ~25 shannons/KW — 40× below the configured `min_fee_rate` of 1,000 shannons/KW

This saturates node CPU, delays legitimate transaction processing, and can cause sustained network congestion across all nodes with public RPC access. The attack is repeatable: as transactions are evicted from the pool (correctly, due to low weight-based fee rate), the attacker re-submits to keep the verify queue saturated.

## Likelihood Explanation

- **Entry path**: Standard public `send_transaction` RPC, no privileges required.
- **Ease**: A tight loop in a lock script suffices to consume 70M cycles; minimal witnesses/outputs keep serialized size small.
- **Cost**: 300 shannons per transaction — negligible.
- **Confirmed by code**: The comment "here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly" explicitly acknowledges the approximation, confirming the discrepancy is structural.
- **Repeatability**: Continuous re-submission after eviction sustains the attack indefinitely.

## Recommendation

After `verify_rtx` completes and `verified.cycles` is known, perform a second fee-rate check using the actual weight before creating the `TxEntry`:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let actual_fee_rate = FeeRate::calculate(fee, actual_weight);
if actual_fee_rate < tx_pool_config.min_fee_rate {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
```

This should be inserted in `_process_tx` in `tx-pool/src/process.rs` between lines 734 and 751, after `verified` is obtained and before `TxEntry::new`. The verify queue's `is_full` check should also be evaluated against weight rather than raw serialized size to prevent queue-flooding with cycle-heavy small transactions.

## Proof of Concept

1. Construct a CKB transaction with:
   - A lock script containing a tight computation loop consuming ~70,000,000 cycles
   - Minimal witnesses and outputs so serialized size ≈ 300 bytes
   - Fee = 300 shannons (`min_fee_rate × tx_size / 1000 = 1000 × 300 / 1000`)

2. Submit via `send_transaction` RPC.

3. `check_tx_fee` computes `min_fee = 1000 × 300 / 1000 = 300 shannons`. Fee (300) ≥ min_fee (300) → **admitted to verify queue**.

4. `verify_rtx` runs the script: `verified.cycles ≈ 70,000,000`. No post-verification fee-rate check occurs.

5. `TxEntry` is created with `fee_rate() = FeeRate::calculate(300, max(300, 70_000_000 × 0.000_170_571_4)) ≈ 25 shannons/KW` — 40× below `min_fee_rate`.

6. Repeat ~853,000 times to fill the 256MB verify queue (each tx contributes only 300 bytes). Total forced CPU: ~59.7 trillion cycles. Node verification workers are saturated; legitimate transactions are delayed or dropped.

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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
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

**File:** tx-pool/src/component/verify_queue.rs (L17-18)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

**File:** tx-pool/src/component/verify_queue.rs (L104-106)
```rust
    pub fn is_full(&self, add_tx_size: usize) -> bool {
        add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
    }
```

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```
