### Title
Fee Gate Uses Serialized Size Only, Ignoring Cycle Consumption — Cycle-Heavy Transactions Admitted Below True Cost - (File: `tx-pool/src/util.rs`)

### Summary

The tx-pool admission fee check in `check_tx_fee` computes the minimum required fee using only the transaction's serialized byte size, explicitly ignoring the cycle dimension of transaction weight. Because CKB's true transaction weight is `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`, a transaction with a small serialized size but maximum cycle consumption passes the fee gate at a fraction of the fee that its actual resource cost warrants. No second fee check is performed after cycles are determined. An unprivileged attacker can submit cycle-heavy transactions at the minimum size-based fee, consuming disproportionate verification CPU at near-zero cost.

### Finding Description

CKB defines transaction weight as the maximum of two dimensions:

```
weight = max(tx_size_bytes, cycles × DEFAULT_BYTES_PER_CYCLES)
``` [1](#0-0) 

`DEFAULT_BYTES_PER_CYCLES` is `0.000_170_571_4`, meaning 1 cycle ≈ 0.00017 weight-bytes. [2](#0-1) 

However, the tx-pool admission check `check_tx_fee` deliberately uses **only `tx_size`** (serialized bytes) to compute the minimum fee, as the comment itself acknowledges:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [3](#0-2) 

This check runs during `pre_check`, before script execution determines actual cycles. [4](#0-3) 

After `verify_rtx` returns the actual cycles, the code creates a `TxEntry` with those cycles but performs **no second fee check** against the cycle-adjusted weight: [5](#0-4) 

The `TxEntry` stores the real cycles and the `fee_rate()` method correctly uses `get_transaction_weight(size, cycles)`: [6](#0-5) 

But this correct weight is only used for **eviction ordering and fee estimation**, never for admission gating.

### Impact Explanation

Consider a transaction with:
- Serialized size: ~200 bytes (minimal inputs/outputs, tiny script)
- Actual cycle consumption: 70,000,000 (the default `max_tx_verify_cycles`)

Fee gate check (size-only):
- `min_fee = 1000 shannons/KW × 200 bytes / 1000 = 200 shannons`

Correct weight-based minimum fee:
- `weight = max(200, 70_000_000 × 0.000_170_571_4) = max(200, 11_940) = 11_940`
- `min_fee = 1000 × 11_940 / 1000 = 11_940 shannons`

The attacker pays **200 shannons** instead of **11,940 shannons** — approximately 60× underpriced. An attacker can flood the tx-pool with cycle-heavy transactions at ~1.7% of the correct cost, exhausting the verification worker pool and degrading or halting transaction processing for legitimate users. [7](#0-6) 

### Likelihood Explanation

The attack requires only:
1. A valid CKB transaction with a lock/type script that consumes many cycles (e.g., a tight computation loop in CKB-VM)
2. Sufficient CKB capacity to pay the (underpriced) fee
3. Access to the RPC `send_transaction` endpoint or a peer connection for relay

No privileged access, no key leakage, no majority hashpower. The RPC endpoint is the standard entry path for any transaction sender. [8](#0-7) 

### Recommendation

After `verify_rtx` returns the actual cycle count, perform a second fee adequacy check using the full weight:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(...));
}
```

This requires reading the tx-pool config inside `_process_tx` after verification, or passing the config into a post-verification fee check. The existing `check_tx_fee` can remain as the cheap pre-check (to reject obviously underpaying transactions early), but a weight-accurate check must follow once cycles are known.

### Proof of Concept

1. Write a CKB script that executes a tight loop consuming ~70,000,000 cycles.
2. Deploy it as a lock script on a cell with minimal capacity.
3. Craft a transaction spending that cell: serialized size ≈ 200 bytes.
4. Submit via `send_transaction` RPC with fee = 200 shannons (satisfies size-only check at 1000 shannons/KW).
5. Observe the transaction is accepted into the pool despite consuming 70M cycles of verification work.
6. Repeat with many UTXOs in parallel; each submission triggers a full 70M-cycle VM execution at a fee 60× below the weight-correct minimum.
7. The verification worker pool saturates; legitimate transactions queue indefinitely. [9](#0-8) [1](#0-0)

### Citations

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

**File:** tx-pool/src/process.rs (L371-378)
```rust
    pub(crate) async fn submit_remote_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Cycle,
        peer: PeerIndex,
    ) -> Result<bool, Reject> {
        self.resumeble_process_tx_and_notify_full_reject(tx, false, Some((declared_cycles, peer)))
            .await
```

**File:** tx-pool/src/process.rs (L715-717)
```rust
        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);
```

**File:** tx-pool/src/process.rs (L736-751)
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
        }

        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L14-14)
```rust
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
```
