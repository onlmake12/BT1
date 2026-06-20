### Title
`check_tx_fee` Uses Serialized Size Instead of Transaction Weight for Fee Rate Enforcement, Allowing High-Cycle Transactions to Bypass `min_fee_rate` — (`tx-pool/src/util.rs`)

---

### Summary

`check_tx_fee` computes the minimum required fee using only the transaction's serialized byte size, but `min_fee_rate` is defined in shannons per kilo-**weight**, where weight is `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For transactions with high cycle counts but small serialized size, the size-only check produces a minimum fee far below what the actual weight-based rate requires. There is no subsequent re-check after cycles are known. Any RPC caller or P2P relayer can exploit this to admit high-cycle transactions into the pool at a fraction of the intended minimum fee.

---

### Finding Description

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate at pool admission time:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { ... }
``` [1](#0-0) 

`FeeRate` is defined as **shannons per kilo-weight**, and `get_transaction_weight` computes the true weight as:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [3](#0-2) 

`check_tx_fee` is called in `pre_check` before cycles are known: [4](#0-3) 

After `verify_rtx` returns the actual cycles, a `TxEntry` is created with the real cycle count, but **no second fee rate check is performed**: [5](#0-4) 

`TxEntry::fee_rate()` correctly uses `get_transaction_weight(size, cycles)`: [6](#0-5) 

This creates a unit mismatch: admission uses `size` (bytes), but the rate is defined in shannons/kilo-**weight**. For cycle-heavy transactions, `weight >> size`, so the admission check is far too permissive.

---

### Impact Explanation

**Concrete example** with default config (`min_fee_rate = 1000` shannons/KB, `max_tx_verify_cycles = 70_000_000`):

| Parameter | Value |
|---|---|
| Serialized size | 100 bytes |
| Cycles | 70,000,000 |
| True weight | `max(100, 70_000_000 × 0.000_170_571_4)` = **11,940** |
| Fee required by `check_tx_fee` | `1000 × 100 / 1000` = **100 shannons** |
| Fee required by actual weight | `1000 × 11,940 / 1000` = **11,940 shannons** |

An attacker pays **~119× less** than the intended minimum. This allows:
- Cheap flooding of the tx-pool with cycle-expensive transactions
- Miners receive far less fee than `min_fee_rate` implies for cycle-heavy work
- Fee rate statistics (`get_fee_rate_statistics` RPC) are skewed, since they use the correct weight-based rate for confirmed transactions

---

### Likelihood Explanation

Any unprivileged RPC caller (`send_transaction`) or P2P peer relaying a transaction can trigger this. Crafting a transaction with a large script (high cycles) and minimal outputs (small size) is straightforward. The default `max_tx_verify_cycles = 70_000_000` provides a large amplification factor. [7](#0-6) 

---

### Recommendation

Replace the size-only check in `check_tx_fee` with the actual weight. Since cycles are not yet known at `pre_check` time, use a declared-cycles value (already passed as `declared_cycles` in `_process_tx`) or apply the weight-based check after `verify_rtx` returns the confirmed cycle count:

```rust
// After verify_rtx returns `verified.cycles`:
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee { return Err(Reject::LowFeeRate(...)); }
```

Alternatively, if a declared cycle count is available at admission time, use it to compute a tighter bound in `check_tx_fee`.

---

### Proof of Concept

1. Craft a transaction with a script that consumes ~70M cycles but has a small serialized size (~100 bytes).
2. Set the fee to 101 shannons (just above `min_fee_rate.fee(100) = 100`).
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1000 × 100 / 1000 = 100 shannons` → passes.
5. `verify_rtx` confirms 70M cycles; `TxEntry` is created with `fee_rate() = FeeRate::calculate(101, 11940) ≈ 8 shannons/KW` — far below `min_fee_rate = 1000`.
6. Transaction is admitted to the pool. Repeat to flood the pool with cycle-expensive, nearly-free transactions. [8](#0-7) [9](#0-8)

### Citations

**File:** tx-pool/src/util.rs (L28-53)
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

**File:** tx-pool/src/process.rs (L289-290)
```rust
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
```

**File:** tx-pool/src/process.rs (L715-753)
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

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
```

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L10-10)
```rust
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
```
