Audit Report

## Title
Tx-Pool Admission Check Uses `tx_size` Instead of Actual Weight, Allowing Cycle-Heavy Transactions to Bypass `min_fee_rate` — (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using only the serialized byte size (`tx_size`) as weight, while the canonical weight used everywhere else in the pool is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For cycle-heavy transactions, these two values diverge by up to ~60×. Because no second fee-rate check is performed after `verify_rtx` produces the actual cycle count, cycle-heavy transactions enter the pool and are relayed to peers at a fraction of the intended minimum fee rate, effectively bypassing the `min_fee_rate` anti-spam mechanism.

## Finding Description

**Root cause — `check_tx_fee`**

The function explicitly uses `tx_size` as weight with a comment acknowledging the limitation:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { … }
``` [1](#0-0) 

`FeeRate::fee(weight)` computes `fee_rate * weight / 1000`: [2](#0-1) 

So `min_fee = min_fee_rate × tx_size / 1000`, ignoring cycles entirely.

**Canonical weight definition**

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [3](#0-2) 

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`: [4](#0-3) 

**No second fee-rate check after verification**

In the primary submission path (`pre_check`), `check_tx_fee` is called before `verify_rtx`. After `verify_rtx` returns the actual cycle count, `TxEntry` is created with real cycles but no second fee-rate check is performed: [5](#0-4) [6](#0-5) 

The same pattern repeats in `readd_detached_tx`. All subsequent fee-rate comparisons (sorting, eviction, statistics) correctly use `get_transaction_weight(size, cycles)`: [7](#0-6) 

**Concrete numeric example**

| Parameter | Value |
|---|---|
| `tx_size` | 200 bytes |
| `cycles` | 70,000,000 (max) |
| `actual_weight` | `max(200, 70M × 0.000_170_571_4)` = **11,940** |
| `min_fee_rate` | 1,000 shannons/KW (default) |
| `min_fee` from `check_tx_fee` | `1,000 × 200 / 1,000` = **200 shannons** |
| Effective fee rate with fee=200 | `200 × 1,000 / 11,940` ≈ **16.7 shannons/KW** |

The transaction passes the admission gate paying ~60× less than the minimum required fee rate.

## Impact Explanation

This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**. An attacker can flood the tx-pool and P2P relay network with cycle-heavy transactions at ~1/60th of the intended minimum cost. These transactions enter the pool legitimately, are relayed to all peers (which accept them via the same lenient gate), occupy pool space, and displace legitimate transactions during eviction. The `min_fee_rate` anti-spam mechanism is rendered ineffective for the entire cycle-heavy transaction class.

## Likelihood Explanation

No privilege is required. Any node's RPC endpoint (`send_transaction`) or P2P relay path accepts the transaction. Crafting a small-serialized, high-cycle transaction (a script with a tight loop) is straightforward. The default `max_tx_verify_cycles = 70,000,000` gives a worst-case weight ratio of ~60×, making the bypass significant and reliable. The attack is repeatable and requires no victim interaction.

## Recommendation

After `verify_rtx` returns the actual cycle count, perform a second fee-rate check using the real weight:

```rust
// After verify_rtx:
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let actual_min_fee = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < actual_min_fee {
    return Err(Reject::LowFeeRate(tx_pool.config.min_fee_rate, actual_min_fee.as_u64(), fee.as_u64()));
}
```

This mirrors the pattern already used in `TxEntry::fee_rate()` and the fee-rate statistics collector, both of which call `get_transaction_weight(size, cycles)` correctly. [7](#0-6) 

## Proof of Concept

1. Write a CKB lock script that executes a tight loop consuming ~70M cycles.
2. Build a transaction spending a cell locked by that script; serialized size ~200 bytes.
3. Set `inputs_capacity − outputs_capacity = 200 shannons` (fee).
4. Submit via `send_transaction` RPC to a node with default `min_fee_rate = 1000 shannons/KW`.
5. `check_tx_fee` computes `min_fee = 1,000 × 200 / 1,000 = 200 shannons` → **passes**.
6. `verify_rtx` confirms actual cycles ≈ 70M; `TxEntry` is created with `actual_weight ≈ 11,940`.
7. `entry.fee_rate()` = `200 × 1,000 / 11,940 ≈ 16.7 shannons/KW` — far below the 1,000 shannons/KW minimum.
8. The transaction is in the pool and relayed to peers, all of which accept it through the same lenient gate.
9. Repeat to fill the pool with near-zero-cost cycle-heavy transactions.

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

**File:** util/types/src/core/tx_pool.rs (L279-279)
```rust
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

**File:** tx-pool/src/process.rs (L286-294)
```rust
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
```

**File:** tx-pool/src/process.rs (L895-906)
```rust
                if let Ok(verified) = verify_rtx(
                    snapshot,
                    Arc::clone(&rtx),
                    tx_env,
                    &verify_cache,
                    max_cycles,
                    None,
                )
                .await
                {
                    let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
                    if let Err(e) = _submit_entry(tx_pool, status, entry, &self.callbacks) {
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```
