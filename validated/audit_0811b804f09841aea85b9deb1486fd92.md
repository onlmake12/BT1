### Title
Tx-Pool Admission Check Uses Raw `tx_size` as Weight Instead of Actual Weight, Allowing Cycle-Heavy Transactions to Bypass `min_fee_rate` — (File: `tx-pool/src/util.rs`)

---

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using only the serialized byte size (`tx_size`) as the weight. However, the canonical weight used everywhere else in the pool is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For cycle-heavy transactions the two values diverge by up to ~60×, so the admission gate accepts transactions whose effective fee rate is far below `min_fee_rate`. This is the direct CKB analog of the Zaros "value not scaled to the correct precision before arithmetic" class.

---

### Finding Description

**Root cause — `check_tx_fee`** [1](#0-0) 

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { … }
```

`FeeRate::fee(weight)` computes `fee_rate * weight / KW` (KW = 1000). [2](#0-1) 

So `min_fee = min_fee_rate × tx_size / 1000`.

**Canonical weight definition** [3](#0-2) 

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [4](#0-3) 

**Process flow — no second fee-rate check after verification**

After `check_tx_fee` passes, `verify_rtx` runs and produces the real cycle count. The entry is then created with the actual cycles, but no second fee-rate check is performed: [5](#0-4) 

The `TxEntry` stores the real cycles and uses `get_transaction_weight` for all subsequent fee-rate comparisons (sorting, eviction, statistics): [6](#0-5) 

**Concrete numeric example**

| Parameter | Value |
|---|---|
| `tx_size` | 200 bytes |
| `cycles` | 70 000 000 (= `max_tx_verify_cycles`) |
| `actual_weight` | `max(200, 70 000 000 × 0.000 170 571 4)` = **11 940** |
| `min_fee_rate` | 1 000 shannons/KW (default) |
| `min_fee` from `check_tx_fee` | `1 000 × 200 / 1 000` = **200 shannons** |
| Actual fee rate with fee = 200 | `200 × 1 000 / 11 940` ≈ **16.7 shannons/KW** |

The transaction passes the admission gate paying ~60× less than the minimum required fee rate.

---

### Impact Explanation

Any unprivileged RPC caller (`send_transaction`) or P2P relay peer can submit cycle-heavy transactions whose effective fee rate is far below `min_fee_rate`. These transactions:

1. Enter the pool legitimately (pass `check_tx_fee`).
2. Are relayed to peers, which also accept them via the same lenient check.
3. Occupy pool space and consume relay bandwidth at a fraction of the intended cost.
4. Are only evicted when the pool is full, at which point they displace legitimate transactions with higher effective fee rates.

The `min_fee_rate` anti-spam mechanism is effectively bypassed for the entire cycle-heavy transaction class.

---

### Likelihood Explanation

The attack requires no privilege. Any node's RPC endpoint (`send_transaction`) or P2P relay path accepts the transaction. Crafting a small-serialized, high-cycle transaction (e.g., a script with a tight loop) is straightforward. The default `max_tx_verify_cycles = 70 000 000` gives a worst-case weight ratio of ~60×, making the bypass significant and reliable.

---

### Recommendation

After `verify_rtx` returns the actual cycle count, perform a second fee-rate check using the real weight:

```rust
// After verify_rtx:
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let actual_min_fee = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < actual_min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

This mirrors the pattern used in `TxEntry::fee_rate()` and `FeeRateCollector::statistics()`, both of which already call `get_transaction_weight(size, cycles)` correctly. [7](#0-6) [8](#0-7) 

---

### Proof of Concept

1. Write a CKB lock script that executes a tight loop consuming ~70 M cycles.
2. Build a transaction spending a cell locked by that script; the serialized transaction is ~200 bytes.
3. Set the output capacity so that `inputs_capacity − outputs_capacity = 200 shannons` (fee).
4. Submit via `send_transaction` RPC.
5. `check_tx_fee` computes `min_fee = 1 000 × 200 / 1 000 = 200 shannons` → **passes**.
6. `verify_rtx` confirms actual cycles ≈ 70 M; entry is created with `actual_weight ≈ 11 940`.
7. `entry.fee_rate()` = `200 × 1 000 / 11 940 ≈ 16.7 shannons/KW` — far below the 1 000 shannons/KW minimum.
8. The transaction is now in the pool and relayed to peers, all of which accept it through the same lenient gate.

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

**File:** tx-pool/src/process.rs (L886-906)
```rust
            let tx_size = tx.data().serialized_size_in_block();
            let tx_hash = tx.hash();
            if let Ok((rtx, status)) = resolve_tx(tx_pool, tx_pool.snapshot(), tx, false)
                && let Ok(fee) = check_tx_fee(tx_pool, tx_pool.snapshot(), &rtx, tx_size)
            {
                let verify_cache = fetched_cache.get(&tx_hash).cloned();
                let snapshot = tx_pool.cloned_snapshot();
                let tip_header = snapshot.tip_header();
                let tx_env = Arc::new(status.with_env(tip_header));
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

**File:** rpc/src/util/fee_rate.rs (L103-106)
```rust
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
                        }
```
