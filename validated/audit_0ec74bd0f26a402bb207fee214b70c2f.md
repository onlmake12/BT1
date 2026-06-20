### Title
Min Fee Rate Admission Check Uses Size-Only Weight, Allowing Sub-Minimum Effective Fee Rate Transactions Into the Pool — (`tx-pool/src/util.rs`)

---

### Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the `min_fee_rate` threshold using only the transaction's serialized byte size as weight. However, the actual weight used for block assembly and pool eviction is `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. A transaction with high cycle count and small serialized size passes the admission check while its true effective fee rate is far below the configured minimum — directly analogous to UToken's pre-operation minimum check that ignores the post-operation actual amount.

---

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` computes the minimum required fee as:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { return Err(Reject::LowFeeRate(...)); }
``` [1](#0-0) 

The code itself acknowledges the discrepancy with the comment: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly."*

The actual weight used everywhere else in the system — block assembly priority, pool eviction scoring, and `TxEntry::fee_rate()` — is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
``` [2](#0-1) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [3](#0-2) 

`TxEntry::fee_rate()` uses this full weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
``` [4](#0-3) 

The admission check (`check_tx_fee`) runs inside `pre_check`, which executes **before** `verify_rtx` determines the actual cycles:

```rust
let (ret, snapshot) = self.pre_check(&tx).await;
let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);
// ... verify_rtx runs here, producing verified.cycles
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
``` [5](#0-4) 

After `verify_rtx` returns the actual cycles, **no second fee-rate check is performed** using the true weight. The transaction is unconditionally admitted if the size-only check passed.

**Concrete example** with default config (`min_fee_rate = 1000 shannons/KB`, `max_tx_verify_cycles = 70,000,000`):

| Parameter | Value |
|---|---|
| Transaction serialized size | 200 bytes |
| Cycles consumed | 70,000,000 |
| Size-based min fee (admission check) | `1000 × 200 / 1000 = 200 shannons` |
| Actual weight | `max(200, 70,000,000 × 0.000_170_571_4) = 11,940 bytes` |
| Effective fee rate at 200 shannons | `200 × 1000 / 11,940 ≈ 16.7 shannons/KB` |
| Ratio below minimum | **~60×** |

The maximum weight inflation is bounded by `max_tx_verify_cycles` (70M cycles → ~11,940 bytes weight), giving a worst-case bypass factor of approximately `11,940 / min_tx_size`.

---

### Impact Explanation

An unprivileged tx-pool submitter can continuously inject transactions whose effective fee rate is up to ~60× below the operator-configured `min_fee_rate`. These transactions:

1. **Occupy pool space** — admitted and held in the pending pool until evicted.
2. **Degrade pool quality** — the `min_fee_rate` configuration is intended to keep low-quality transactions out; this bypass undermines that guarantee.
3. **Pollute fee estimation** — the fee estimator (`accept_tx`) records admitted transactions; low-effective-fee-rate transactions skew fee rate statistics downward.
4. **Require continuous resubmission to maintain** — the eviction mechanism in `limit_size` uses the actual `fee_rate()` (which accounts for cycles), so these transactions are evicted first when the pool fills. However, the attacker can resubmit them continuously at negligible cost (200 shannons per transaction).

The pool's eviction mechanism partially mitigates the impact but does not prevent admission.

---

### Likelihood Explanation

- Entry path: any unprivileged caller of the `send_transaction` RPC or P2P relay.
- Requires only: a valid transaction with high cycle count (e.g., a script that loops near `max_tx_verify_cycles`) and a fee set just above `min_fee_rate × tx_size`.
- No special privileges, keys, or majority hashpower required.
- The attack is cheap: the attacker pays only the size-based minimum fee, which is far below what the operator intended to require.

---

### Recommendation

After `verify_rtx` returns the actual cycles, add a post-verification fee rate check using the true weight:

```rust
// After: let verified = try_or_return_with_snapshot!(verified_ret, snapshot);
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_actual = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < min_fee_actual {
    return Some((
        Err(Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee_actual.as_u64(), fee.as_u64())),
        snapshot,
    ));
}
```

This mirrors the fix suggested in the UToken report: move the threshold check to after the actual result is known, rather than relying solely on the pre-operation declared value.

---

### Proof of Concept

1. Craft a CKB script that consumes close to `max_tx_verify_cycles` (70M) cycles — e.g., a tight loop in RISC-V.
2. Build a transaction spending a live cell, with the script as the lock, serialized size ~200 bytes.
3. Set the output capacity so that `inputs_capacity - outputs_capacity = 200 shannons` (just above `min_fee_rate × 200 bytes / 1000`).
4. Submit via `send_transaction` RPC.
5. Observe the transaction is accepted into the pool (`get_transaction` returns `Pending`).
6. Query `get_pool_tx_detail_info` — the `score_sortkey.fee` will show 200 shannons but the `weight` will reflect ~11,940 bytes, confirming the effective fee rate is ~16.7 shannons/KB, far below the 1000 shannons/KB minimum.
7. Repeat in a loop; each submission costs only 200 shannons while occupying pool space intended for transactions paying ≥1000 shannons/KB.

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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/process.rs (L715-751)
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
```
