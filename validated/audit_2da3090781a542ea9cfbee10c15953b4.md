### Title
`check_tx_fee` Uses `tx_size` as Weight Instead of Proper Transaction Weight, Allowing CPU-Heavy Transactions to Bypass Minimum Fee Rate Admission — (`tx-pool/src/util.rs`)

---

### Summary

In `tx-pool/src/util.rs`, the `check_tx_fee` function enforces the minimum fee rate gate for tx-pool admission. It computes the minimum required fee using only the serialized byte size of the transaction (`tx_size`) as the weight, rather than the correct composite weight `get_transaction_weight(tx_size, cycles)`. This is structurally identical to the Allora M-32 bug: a wrong value is substituted into a threshold calculation, causing the gate to be evaluated against an incorrect denominator and allowing transactions that should be rejected to be admitted.

---

### Finding Description

CKB's transaction weight is defined as:

```
weight = max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)
```

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. For a CPU-heavy transaction with `cycles = 70_000_000` (the `max_tx_verify_cycles` default), the weight contribution from cycles alone is `≈ 11,940` bytes, which can dwarf a small serialized size.

The `check_tx_fee` function, however, computes the minimum required fee using only `tx_size`:

```rust
// tx-pool/src/util.rs:42-45
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

`FeeRate::fee` is defined as `fee = rate * weight / 1000`: [2](#0-1) 

So `min_fee = min_fee_rate * tx_size / 1000`. For a transaction with a small serialized body but many cycles, this minimum is far below what the correct weight-based minimum would be.

The correct weight function is: [3](#0-2) 

After `check_tx_fee` passes, `verify_rtx` is called and the actual cycles are obtained. A `TxEntry` is then created with the real cycles: [4](#0-3) 

But there is **no second fee rate check** after verification using the proper weight. The transaction is admitted with whatever fee it paid, as long as it cleared the size-only cheap check. The entry's `fee_rate()` method does use the correct weight: [5](#0-4) 

— but this is only used for sorting and eviction, not for admission gating.

The `check_tx_fee` function is called in `pre_check` before verification: [6](#0-5) 

---

### Impact Explanation

An unprivileged transaction sender can craft a CPU-heavy transaction with:
- A small serialized size (e.g., 200 bytes → `min_fee = 200 shannons` at 1000 shannons/KB)
- Many cycles (e.g., 70,000,000 → proper weight ≈ 11,940 → proper `min_fee = 11,940 shannons`)
- A fee of, say, 201 shannons — passing the cheap check but far below the correct minimum

Such a transaction is admitted to the tx-pool. The node then spends full CPU cycles verifying it. An attacker can repeat this to:
1. Force the node to perform expensive script verification on transactions that should have been rejected at the fee gate
2. Fill the tx-pool with low-effective-fee-rate transactions, displacing legitimate ones until eviction runs

The tx-pool is not consensus-critical, so this does not affect chain validity, but it is a reachable DoS vector for any node accepting transactions via RPC (`send_transaction`) or P2P relay.

---

### Likelihood Explanation

Any unprivileged RPC caller or P2P relay peer can submit transactions. Crafting a CPU-heavy transaction with a small serialized body is straightforward — a script that loops for many cycles but has minimal witness/output data is sufficient. The attacker only needs to declare the correct cycle count (which they know, since they wrote the script) to avoid `DeclaredWrongCycles` rejection. [7](#0-6) 

---

### Recommendation

After `verify_rtx` returns the actual `verified.cycles`, perform a second fee rate check using the proper composite weight before admitting the entry:

```rust
// After verify_rtx, before submit_entry:
let proper_weight = get_transaction_weight(tx_size, verified.cycles);
let proper_min_fee = tx_pool.config.min_fee_rate.fee(proper_weight);
if fee < proper_min_fee {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

This mirrors the correct pattern already used in `TxEntry::fee_rate()` and `FeeRateCollector::statistics()`: [8](#0-7) 

---

### Proof of Concept

1. Write a CKB lock script that executes a tight loop consuming ~70,000,000 cycles but serializes to ~200 bytes.
2. Submit a transaction spending a cell locked by this script, paying a fee of 201 shannons (above `min_fee_rate.fee(200) = 200` at 1000 shannons/KB).
3. Declare the correct cycle count.
4. Observe: `check_tx_fee` passes (201 > 200), `verify_rtx` passes (cycles match), the transaction is admitted to the tx-pool despite having an effective fee rate of `201 * 1000 / 11940 ≈ 16 shannons/KB` — far below the 1000 shannons/KB minimum.
5. Repeat to exhaust node CPU and tx-pool capacity. [9](#0-8)

### Citations

**File:** tx-pool/src/util.rs (L28-54)
```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
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
}
```

**File:** util/types/src/core/fee_rate.rs (L34-37)
```rust
    pub fn fee(self, weight: u64) -> Capacity {
        let fee = self.0.saturating_mul(weight) / KW;
        Capacity::shannons(fee)
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

**File:** tx-pool/src/process.rs (L289-290)
```rust
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
```

**File:** tx-pool/src/process.rs (L736-748)
```rust
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
```

**File:** tx-pool/src/process.rs (L751-753)
```rust
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

**File:** rpc/src/util/fee_rate.rs (L103-105)
```rust
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
```
