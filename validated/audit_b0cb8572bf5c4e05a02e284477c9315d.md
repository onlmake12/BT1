### Title
`check_tx_fee` Validates Fee Rate Against `tx_size` But Effective Fee Rate Uses `get_transaction_weight(size, cycles)` — (File: `tx-pool/src/util.rs`)

---

### Summary

The `check_tx_fee` function in the tx-pool admission path enforces `min_fee_rate` by computing `min_fee` solely from the transaction's serialized byte size (`tx_size`). However, once the transaction is verified and its actual cycle count is known, the pool stores and sorts entries by their true **weight** — `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. No second fee-rate check is performed after cycles are known. A transaction submitter can craft a small-serialized-size but high-cycle transaction that passes the size-only admission check while having an effective fee rate far below `min_fee_rate`.

---

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` computes the minimum required fee using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [1](#0-0) 

This check is invoked in `pre_check` before script execution, when cycles are not yet known:

```rust
let tx_size = tx.data().serialized_size_in_block();
...
let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
``` [2](#0-1) 

After verification completes and actual cycles are known, a `TxEntry` is created with both `size` and `cycles`. Its `fee_rate()` method uses the true weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [3](#0-2) 

Where `get_transaction_weight` is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [4](#0-3) 

There is **no second fee-rate check** after cycles are known. The admission gate (`check_tx_fee`) checks against `tx_size`; the actual metric used for pool prioritization and eviction is `max(tx_size, cycles × 0.000170571)`.

---

### Impact Explanation

An unprivileged RPC caller submitting via `send_transaction` can craft a transaction with:
- Small serialized size (e.g., 200 bytes) → passes `check_tx_fee` with a tiny fee
- High cycle count (e.g., 70 million cycles, within `max_tx_verify_cycles`) → actual weight = `max(200, 70_000_000 × 0.000170571)` = **11,940**

With `min_fee_rate = 1000 shannons/KW`:
- Admission check: `min_fee = 1000 × 200 / 1000 = 200 shannons`. A fee of 201 shannons passes.
- Actual effective fee rate: `201 / 11940 × 1000 ≈ 16.8 shannons/KW` — **~60× below `min_fee_rate`**.

Consequences:
1. The node expends significant CPU verifying a high-cycle script for a near-zero fee.
2. The transaction occupies pool space with an effective fee rate far below the configured minimum, displacing legitimate transactions when the pool is full.
3. An attacker can repeatedly submit such transactions to degrade pool quality and waste node resources, reachable from any RPC caller without any privileged access.

---

### Likelihood Explanation

The `send_transaction` RPC endpoint is publicly accessible to any node operator or connected client. Crafting a transaction with a small serialized body but a script that consumes many cycles is straightforward (e.g., a tight loop in a CKB-VM script). The attacker only needs to pay a fee proportional to `tx_size`, not to the actual computational cost. This is a low-cost, repeatable attack vector.

---

### Recommendation

After script verification completes and the actual cycle count is known, perform a second fee-rate check using the true weight before inserting the entry into the pool:

```rust
// After verify_rtx returns actual cycles:
let weight = get_transaction_weight(tx_size, completed.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

This mirrors the pattern used in `TxEntry::fee_rate()` and closes the gap between the admission check and the actual fee-rate metric.

---

### Proof of Concept

1. Construct a CKB transaction with a lock script that runs a tight loop consuming ~70 million cycles. Keep the serialized transaction size small (e.g., 200 bytes with a minimal script reference via `code_hash`).
2. Set the transaction fee to 201 shannons (just above `min_fee_rate.fee(200) = 200` at 1000 shannons/KW).
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 200 shannons`; fee = 201 > 200 → **admitted**.
5. After verification, actual cycles = 70,000,000. Weight = `max(200, 11940) = 11940`. Effective fee rate = `201/11940×1000 ≈ 16.8 shannons/KW` — far below the 1000 shannons/KW minimum.
6. The transaction occupies pool space and consumed node CPU at a ~60× discount relative to the configured minimum fee rate.

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

**File:** tx-pool/src/process.rs (L274-290)
```rust
        let tx_size = tx.data().serialized_size_in_block();

        let (ret, snapshot) = self
            .with_tx_pool_read_lock(|tx_pool, snapshot| {
                let tip_hash = snapshot.tip_hash();

                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;

                // Try normal path first, if double-spending check success we don't need RBF check
                // this make sure RBF won't introduce extra performance cost for hot path
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
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
