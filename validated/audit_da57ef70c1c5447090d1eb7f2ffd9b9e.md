All cited code references are confirmed in the repository. Let me verify the complete flow:

Audit Report

## Title
Tx-Pool Min-Fee-Rate Admission Check Uses Only Serialized Size, Enabling Cheap CPU-Exhaustion DoS via Cycle-Heavy Transactions — (`tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size, while the actual resource weight is `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. A transaction with ~300 serialized bytes and ~70M cycles passes the admission gate paying only ~300 shannons — roughly 40× below the effective minimum — and forces full script execution with no post-verification fee-rate gate. Because the verify queue's capacity is also measured in raw serialized bytes, an attacker can flood it with such transactions and saturate node CPU at negligible cost.

## Finding Description

**Size-only admission check (`tx-pool/src/util.rs`, lines 42–52):**

`check_tx_fee` computes the minimum required fee using only `tx_size`, with an explicit code comment acknowledging the approximation:

```rust
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { return Err(reject); }
``` [1](#0-0) 

**Correct weight definition (`util/types/src/core/tx_pool.rs`, lines 298–303):**

The actual weight used for pool sorting, eviction, and block assembly accounts for cycles:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
``` [2](#0-1) 

**`TxEntry::fee_rate()` uses the correct weight (`tx-pool/src/component/entry.rs`, lines 114–118):** [3](#0-2) 

This means a 300-byte / 70M-cycle entry has `fee_rate() ≈ 25 shannons/KW` — 40× below the 1,000 shannons/KW `min_fee_rate` — but this is never checked at admission.

**No second fee-rate check after `verify_rtx` (`tx-pool/src/process.rs`, lines 734–751):**

After `verified` is obtained (line 734), `_process_tx` immediately creates the `TxEntry` and calls `submit_entry` with no weight-based fee-rate validation: [4](#0-3) 

**Verify queue capacity measured in serialized bytes (`tx-pool/src/component/verify_queue.rs`, lines 17–18, 104–106):**

```rust
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;

pub fn is_full(&self, add_tx_size: usize) -> bool {
    add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
}
``` [5](#0-4) [6](#0-5) 

A 300-byte transaction contributes only 300 bytes to this limit, allowing ~853,000 such transactions to fill the queue simultaneously.

**Pool eviction also measured in serialized bytes (`tx-pool/src/pool.rs`, line 298):** [7](#0-6) 

A 300-byte / 70M-cycle transaction contributes only 300 bytes to the 180MB pool limit, so it is not evicted until the pool is nearly full in byte terms, despite having a negligible weight-based fee rate.

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker submitting 300-byte / 70M-cycle transactions at 300 shannons each can fill the 256MB verify queue with ~853,000 transactions (256MB ÷ 300 bytes), forcing nodes to execute ~59.7 trillion cycles of script verification at a fee rate ~40× below `min_fee_rate`. This saturates node CPU, delays legitimate transaction processing, and causes sustained network congestion across all nodes with public RPC access. The attack is repeatable: as transactions are evicted from the pool due to low weight-based fee rate, the attacker re-submits to keep the verify queue saturated.

## Likelihood Explanation

- **Entry path**: Standard public `send_transaction` RPC — no privileges required.
- **Ease**: A tight computation loop in a lock script suffices to consume 70M cycles; referencing the script via a dep cell keeps serialized transaction size small (~300 bytes).
- **Cost**: 300 shannons per transaction — negligible.
- **Confirmed by code**: The comment "here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly" explicitly acknowledges the structural discrepancy.
- **Repeatability**: Continuous re-submission after eviction sustains the attack indefinitely.

## Recommendation

After `verify_rtx` completes and `verified.cycles` is known, insert a weight-based fee-rate check in `_process_tx` (`tx-pool/src/process.rs`) between lines 734 and 751, before `TxEntry::new`:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let actual_fee_rate = FeeRate::calculate(fee, actual_weight);
if actual_fee_rate < tx_pool_config.min_fee_rate {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
```

Additionally, the verify queue's `is_full` check in `verify_queue.rs` should evaluate against transaction weight rather than raw serialized size to prevent queue-flooding with cycle-heavy small transactions.

## Proof of Concept

1. Construct a CKB transaction with a lock script (referenced via dep cell) containing a tight computation loop consuming ~70,000,000 cycles, minimal witnesses and outputs so serialized size ≈ 300 bytes, and fee = 300 shannons.
2. Submit via `send_transaction` RPC.
3. `check_tx_fee` computes `min_fee = 1000 × 300 / 1000 = 300 shannons`. Fee (300) ≥ min_fee (300) → admitted to verify queue (300 bytes added to `total_tx_size`).
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

**File:** tx-pool/src/process.rs (L734-753)
```rust
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
