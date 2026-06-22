### Title
Inconsistent Fee Rate Calculation Between Pool Admission Check and Actual Fee Rate Computation — (File: `tx-pool/src/util.rs`)

---

### Summary

The `check_tx_fee` function uses raw serialized `tx_size` as the weight denominator for the `min_fee_rate` admission check, but the actual fee rate stored in `TxEntry` and used for block assembly priority, eviction, and fee-rate statistics uses `get_transaction_weight(size, cycles)` — which is `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For cycle-heavy transactions, the two weights diverge by up to ~60×, allowing a transaction sender to pass the pool admission gate with a fee that is far below the effective `min_fee_rate`.

---

### Finding Description

In `check_tx_fee` the minimum-fee threshold is computed purely from serialized size:

```rust
// tx-pool/src/util.rs  line 45
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code itself acknowledges the inconsistency:

> "Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly" [1](#0-0) 

After the transaction is verified and its actual cycle count is known, `TxEntry` is created with both `size` and `cycles`. Every subsequent fee-rate computation — block-assembly priority, eviction key, and the `get_fee_rate_statistics` RPC — uses the cycle-adjusted weight:

```rust
// tx-pool/src/component/entry.rs  line 115-118
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [2](#0-1) 

`get_transaction_weight` is defined as:

```rust
// util/types/src/core/tx_pool.rs  line 298-303
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [3](#0-2) 

`DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. At the default `max_tx_verify_cycles = 70 000 000`, the cycle-adjusted weight reaches ≈ 11 940 bytes — roughly 60× a minimal 200-byte transaction.

The admission path in `pre_check` computes `tx_size` once and passes it to `check_tx_fee`, then later constructs `TxEntry::new(rtx, verified.cycles, fee, tx_size)` with the real cycle count: [4](#0-3) 

There is no second fee-rate check after cycles are known. The inconsistency is therefore structural: the gate uses one metric (size), while all downstream accounting uses another (weight).

---

### Impact Explanation

**Concrete example** (`min_fee_rate = 1 000 shannons/KW`):

| Parameter | Value |
|---|---|
| `tx_size` | 200 bytes |
| `cycles` | 70 000 000 |
| Admission threshold | `1000 × 200 / 1000 = 200 shannons` |
| Actual weight | `max(200, 11 940) = 11 940` |
| Actual fee rate | `200 × 1000 / 11 940 ≈ 16.7 shannons/KW` |

The transaction is admitted at **~60× below** the intended minimum fee rate.

Downstream effects:
1. **Pool admission bypass** — any tx sender can undercut `min_fee_rate` by maximising cycles relative to size.
2. **Fee-rate statistics skewed** — `get_fee_rate_statistics` RPC (`rpc/src/util/fee_rate.rs`) reads `txs_fees` and `txs_sizes` from `BlockExt` and recomputes fee rates using the proper weight; if such transactions are committed, the reported statistics are pulled downward, misleading wallets and users into setting fees too low.
3. **Inconsistent pool accounting** — the eviction key and ancestor-score sort key both use the cycle-adjusted weight, so the pool internally treats the transaction as low-priority, yet it was admitted as if it met the minimum. [5](#0-4) 

---

### Likelihood Explanation

**High.** Any unprivileged transaction sender can submit a transaction via the `send_transaction` RPC or P2P relay. Crafting a small transaction whose lock/type script iterates a hash function or performs arithmetic to consume near-`max_tx_verify_cycles` cycles is straightforward and requires no special privilege. The default `max_tx_verify_cycles = 70 000 000` and `min_fee_rate = 1 000 shannons/KW` are public parameters, making the exact bypass fee trivially calculable.

---

### Recommendation

After script verification completes and `verified.cycles` is known, perform a second fee-rate check using the actual weight before inserting the entry into the pool:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

This closes the gap between the admission gate and all downstream fee-rate computations, making the two consistent — analogous to computing price impact only after fee deductions are applied.

---

### Proof of Concept

1. Craft a transaction with a lock script that loops a hash function to consume ≈ 70 000 000 cycles. Keep the serialized transaction body small (≈ 200 bytes).
2. Set the output capacity so that `inputs_capacity − outputs_capacity = 200 shannons` (the size-based minimum at `min_fee_rate = 1 000`).
3. Submit via `send_transaction` RPC.
4. The node calls `check_tx_fee(…, tx_size=200)` → `min_fee = 200` → `fee (200) >= min_fee (200)` → **admitted**.
5. After verification, `TxEntry::fee_rate()` returns `200 × 1000 / 11 940 ≈ 16.7 shannons/KW` — far below `min_fee_rate = 1 000`.
6. Query `get_fee_rate_statistics` after the transaction is mined; the returned mean/median fee rate is pulled downward by this transaction's anomalously low effective rate. [6](#0-5) [7](#0-6)

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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/component/entry.rs (L221-231)
```rust
impl From<&TxEntry> for AncestorsScoreSortKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let ancestors_weight = get_transaction_weight(entry.ancestors_size, entry.ancestors_cycles);
        AncestorsScoreSortKey {
            fee: entry.fee,
            weight,
            ancestors_fee: entry.ancestors_fee,
            ancestors_weight,
        }
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

**File:** rpc/src/util/fee_rate.rs (L86-110)
```rust
        let mut fee_rates = self.provider.collect(target, |mut fee_rates, block_ext| {
            let BlockExt {
                txs_sizes,
                cycles,
                txs_fees,
                ..
            } = block_ext;
            let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
            if txs_sizes.len() > 1 && !txs_fees.is_empty() {
                // block_ext.txs_fees's length == block_ext.cycles's length
                // block_ext.txs_fees's length + 1 == txs_sizes's length
                if let Some(cycles) = cycles {
                    for (fee, cycles, size) in itertools::izip!(
                        txs_fees,
                        cycles,
                        txs_sizes.iter().skip(1) // skip cellbase (first element in the Vec)
                    ) {
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
                        }
                    }
                }
            }
            fee_rates
```
