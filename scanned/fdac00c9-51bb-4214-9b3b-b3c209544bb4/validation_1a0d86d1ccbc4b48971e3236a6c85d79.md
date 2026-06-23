### Title
`check_tx_fee` Uses Only Serialized Size as Weight, Allowing Cycle-Heavy Transactions to Bypass the Minimum Fee Rate Check - (File: `tx-pool/src/util.rs`)

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using only the transaction's serialized byte size as the weight. However, the canonical transaction weight used everywhere else in the system — block assembly, pool sorting, fee rate statistics — is `get_transaction_weight(tx_size, cycles) = max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For cycle-dominated transactions, the admission check is disproportionately lenient by up to two orders of magnitude, allowing any RPC caller to flood the tx-pool with high-cycle, low-fee transactions.

### Finding Description

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate at tx-pool admission:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [1](#0-0) 

The `FeeRate::fee` function computes `fee_rate * weight / 1000`, so when `weight = tx_size`, the minimum fee is proportional only to the serialized byte count. [2](#0-1) 

The canonical weight used everywhere else — `TxEntry::fee_rate()`, block assembly scoring, fee rate statistics — is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [3](#0-2) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` (= `MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES`). [4](#0-3) 

`TxEntry::fee_rate()` uses the full weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [5](#0-4) 

The two formulas agree only when `tx_size >= cycles * DEFAULT_BYTES_PER_CYCLES` (size-dominated transactions). For cycle-dominated transactions the admission check is far too lenient.

### Impact Explanation

**Concrete example** with `min_fee_rate = 1000` shannons/KW and `max_tx_verify_cycles = 70,000,000`:

| Parameter | Value |
|---|---|
| `tx_size` | 100 bytes |
| `cycles` | 70,000,000 |
| Actual weight | `max(100, 70_000_000 × 0.000_170_571_4)` = **11,940** |
| Min fee (admission check, size only) | `1000 × 100 / 1000` = **100 shannons** |
| Min fee (correct, weight-based) | `1000 × 11,940 / 1000` = **11,940 shannons** |
| Discrepancy | **~119×** |

An attacker pays 100 shannons to occupy the same block resource as a transaction that should cost 11,940 shannons. This enables:

1. **Tx-pool flooding**: Submitting many high-cycle, low-fee transactions that pass admission but consume disproportionate pool resources and miner verification time.
2. **Fee market distortion**: Accepted transactions have an effective fee rate (by weight) far below `min_fee_rate`, corrupting fee rate statistics and fee estimation outputs.
3. **Miner revenue loss**: Miners who include these transactions receive insufficient fees for the computational cycles consumed.

### Likelihood Explanation

The attack requires only an unprivileged RPC call to `send_transaction`. Any user can craft a transaction whose lock or type script consumes close to `max_tx_verify_cycles` cycles while keeping the serialized transaction small (e.g., a script with a tight loop and minimal witness data). No special privilege, key, or majority hashpower is needed. The entry path is confirmed in `pre_check`:

```rust
let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
``` [6](#0-5) 

The default `max_tx_verify_cycles = 70,000,000` is large enough to produce a ~119× discrepancy, making this practically exploitable on mainnet.

### Recommendation

Replace the size-only weight in `check_tx_fee` with the canonical `get_transaction_weight`. Since cycles are not yet known at the pre-check stage (script execution happens after), the check should either:

1. **Use a declared-cycles upper bound**: Accept a `declared_cycles` hint (already threaded through `_process_tx`) and compute `get_transaction_weight(tx_size, declared_cycles)` for the admission check, then verify the declaration matches actual cycles after execution (this check already exists).
2. **Re-check after verification**: After `verify_rtx` returns the actual `verified.cycles`, perform a second fee check using `get_transaction_weight(tx_size, verified.cycles)` before calling `submit_entry`.

Option 2 is simpler and closes the gap completely:

```rust
// After verify_rtx returns verified.cycles:
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

### Proof of Concept

1. Craft a CKB transaction whose lock script runs a tight loop consuming ~70,000,000 cycles but whose serialized size is ~100 bytes.
2. Set the transaction fee to 100 shannons (passing the size-based check: `1000 × 100 / 1000 = 100`).
3. Submit via `send_transaction` RPC.
4. Observe the transaction is accepted into the tx-pool despite its effective fee rate by weight being `1000 × 100 / 11940 ≈ 8 shannons/KW` — far below the configured `min_fee_rate = 1000 shannons/KW`.
5. Repeat to fill the pool with high-cycle, low-fee transactions at ~119× discount.

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

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
    }
```

**File:** util/types/src/core/tx_pool.rs (L276-279)
```rust
/// Equal to MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES, see ckb-chain-spec.
/// The precision is set so that the difference between MAX_BLOCK_CYCLES * DEFAULT_BYTES_PER_CYCLES
/// and MAX_BLOCK_BYTES is less than 1.
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;
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

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/process.rs (L289-290)
```rust
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
```
