### Title
Cycle-Blind Minimum Fee Check Allows Cycle-Griefing of Miners and Mempool — (`tx-pool/src/util.rs`)

---

### Summary

The tx-pool admission check in `check_tx_fee` enforces the minimum fee rate using only the serialized transaction size, ignoring script execution cycles. Because the true transaction weight is `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`, a transaction sender can craft a small-size, high-cycle transaction that passes the minimum fee check while having an effective fee rate up to ~12× below the enforced minimum. This allows an unprivileged attacker to flood the mempool with transactions that impose disproportionate computational cost on verifying nodes and miners relative to the fee paid.

---

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` enforces the minimum fee rate using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
``` [1](#0-0) 

However, the canonical transaction weight used everywhere else in the system is defined in `util/types/src/core/tx_pool.rs`:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [3](#0-2) 

This weight formula is used correctly in `TxEntry::fee_rate()`, the pool's score/eviction keys, and the fee estimator — but **not** at admission time. [4](#0-3) 

The pool config sets `max_tx_verify_cycles = 70_000_000` per transaction. [5](#0-4) 

**Concrete discrepancy:**

| Parameter | Value |
|---|---|
| `tx_size` | 1,000 bytes |
| `cycles` | 70,000,000 (pool max) |
| `actual_weight` | `max(1000, 70,000,000 × 0.000_170_571_4)` = **11,940** |
| `min_fee_rate` | 1,000 shannons/KB |
| Fee required by `check_tx_fee` | `1,000 shannons` (size-only) |
| Effective fee rate at actual weight | `1,000 × 1,000 / 11,940 ≈ 84 shannons/KB` |

A transaction paying exactly the minimum fee passes admission but has an effective fee rate **~12× below** the configured minimum.

The code comment in `check_tx_fee` itself acknowledges this is theoretically incorrect:

> "Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check" [6](#0-5) 

---

### Impact Explanation

1. **Miner cycle-griefing**: Miners select transactions using `get_transaction_weight` (cycles-aware), so high-cycle, low-fee transactions are correctly ranked low for inclusion. However, they still consume full verification cycles during tx-pool processing and block template assembly. Miners who do include them receive far less fee per unit of computational work than the `min_fee_rate` is supposed to guarantee.

2. **Verification worker exhaustion**: Each admitted transaction is verified up to `max_tx_verify_cycles = 70,000,000` cycles by the pool's async verify workers. An attacker paying the minimum size-based fee can submit many such transactions, saturating the verification queue with maximum-cycle work at a fraction of the intended cost.

3. **Mempool fee-rate floor bypass**: The `min_fee_rate` is intended as a spam-prevention floor. The cycle-blind check allows transactions with effective fee rates as low as `min_fee_rate / 12` to enter the pool, undermining the economic spam-prevention guarantee.

---

### Likelihood Explanation

Likelihood is **medium-high**. The attack requires no special privilege — any RPC caller or P2P peer can submit transactions. Crafting a transaction with a small serialized body but a high-cycle lock or type script is straightforward for any script author. The attacker only needs to pay the size-based minimum fee (e.g., 1,000 shannons for a 1 KB transaction), which is negligible. The pool's `max_tx_verify_cycles` cap limits per-transaction damage but does not prevent repeated submission of many such transactions.

---

### Recommendation

Replace the size-only minimum fee check in `check_tx_fee` with a weight-based check that accounts for cycles. Since cycles are not yet known at the pre-check stage (they are computed during script verification), the check should be applied **after** verification using the verified cycle count:

```rust
// After verify_rtx returns verified.cycles:
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

This mirrors how `TxEntry::fee_rate()` already computes the effective fee rate and closes the gap between the admission check and the actual resource cost. [7](#0-6) 

---

### Proof of Concept

1. Craft a CKB transaction with:
   - Minimal serialized size (~1,000 bytes)
   - A lock script that loops for exactly `max_tx_verify_cycles - 1` cycles (e.g., a tight RISC-V loop in a custom script binary referenced via `cell_dep`)
   - Fee = `min_fee_rate × tx_size / 1000` shannons (e.g., 1,000 shannons at 1 KB)

2. Submit via `send_transaction` RPC.

3. Observe: the transaction is accepted into the pool (`check_tx_fee` passes because `fee ≥ min_fee_rate × tx_size / 1000`).

4. Compute the effective fee rate: `fee × 1000 / get_transaction_weight(tx_size, cycles)` — this will be approximately `1000 × 1000 / 11940 ≈ 84 shannons/KB`, far below the 1,000 shannons/KB minimum.

5. Repeat with many such transactions to saturate the verification worker pool and fill the mempool with below-minimum-effective-fee-rate transactions, all while paying only the size-based minimum fee. [8](#0-7) [9](#0-8)

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

**File:** util/types/src/core/tx_pool.rs (L276-303)
```rust
/// Equal to MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES, see ckb-chain-spec.
/// The precision is set so that the difference between MAX_BLOCK_CYCLES * DEFAULT_BYTES_PER_CYCLES
/// and MAX_BLOCK_BYTES is less than 1.
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

/// vbytes has been deprecated, renamed to weight to prevent ambiguity
#[deprecated(
    since = "0.107.0",
    note = "Please use the get_transaction_weight instead"
)]
pub fn get_transaction_virtual_bytes(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}

/// The miners select transactions to fill the limited block space which gives the highest fee.
/// Because there are two different limits, serialized size and consumed cycles,
/// the selection algorithm is a multi-dimensional knapsack problem.
/// Introducing the transaction weight converts the multi-dimensional knapsack to a typical knapsack problem,
/// which has a simple greedy algorithm.
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

**File:** util/app-config/src/configs/tx_pool.rs (L20-21)
```rust
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
```

**File:** tx-pool/src/process.rs (L705-751)
```rust
    pub(crate) async fn _process_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Option<Cycle>,
        command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
    ) -> Option<(Result<Completed, Reject>, Arc<Snapshot>)> {
        let wtx_hash = tx.witness_hash();
        let instant = Instant::now();
        let is_sync_process = command_rx.is_none();

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
