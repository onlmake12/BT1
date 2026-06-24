Audit Report

## Title
Tx-Pool Admission Uses Size-Only Fee Check, Bypassing Cycle-Based Weight — Allows Sub-`min_fee_rate` Transactions Into Pool - (File: `tx-pool/src/util.rs`)

## Summary
`check_tx_fee` in `tx-pool/src/util.rs` enforces `min_fee_rate` using only serialized transaction size, while the canonical weight used for eviction and sorting is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For high-cycles transactions, cycles dominate the true weight by up to ~60×. An unprivileged attacker can craft a transaction that passes the size-only admission gate but whose true fee rate is far below `min_fee_rate`, causing it to be admitted to the pool and relayed to all peers while consuming pool space with an unmineable transaction.

## Finding Description
`check_tx_fee` at `tx-pool/src/util.rs` L45 computes the minimum required fee as:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code comment at L42–44 explicitly acknowledges this is intentional as a "cheap check." However, the canonical weight function `get_transaction_weight` in `util/types/src/core/tx_pool.rs` L298–303 is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

The full pipeline in `_process_tx` (`tx-pool/src/process.rs` L705–777) is:
1. L715: `pre_check` → calls `check_tx_fee(tx_size)` — size-only gate, cycles unknown
2. L724–732: `verify_rtx` — determines actual cycles
3. L751: `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry created with real cycles
4. L753: `submit_entry` — admitted with **no second fee-rate check**

After `submit_entry`, `TxEntry::fee_rate()` (`tx-pool/src/component/entry.rs` L115–117) uses the true weight:

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

But this is only consulted for eviction and sorting, never for admission gating. There is no second fee-rate check after cycles are known. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

## Impact Explanation
An attacker submits a transaction with `tx_size ≈ 200` bytes and `cycles = 70,000,000` (the `max_tx_verify_cycles` default), paying `fee = 201 shannons`:

- Size-only check: `201 >= 1000 * 200 / 1000 = 200` → **passes**
- True weight: `max(200, 70,000,000 × 0.000_170_571_4) = 11,940`
- True fee rate: `201 × 1000 / 11,940 ≈ 16 shannons/KW` — **~62× below `min_fee_rate`**

The transaction is admitted, relayed to all peers, and occupies pool space. Miners will not include it. Continuously submitting such transactions keeps the pool polluted with unmineable entries, degrades mempool quality, and wastes relay bandwidth across the network. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. [5](#0-4) 

## Likelihood Explanation
Any unprivileged user with access to the `send_transaction` RPC endpoint can exploit this. Crafting a high-cycles transaction requires only deploying a script that performs expensive computation up to the cycle limit — no special privileges, keys, or network position are required. The `max_tx_verify_cycles` default of `70,000,000` is reachable by any script author. The attack is repeatable as long as the attacker holds valid UTXOs and pays the (artificially low) size-based fee. [6](#0-5) 

## Recommendation
After `verify_rtx` returns the actual cycle count, perform a second fee-rate check using the true weight before calling `submit_entry`:

```rust
let true_weight = get_transaction_weight(tx_size, verified.cycles);
let true_min_fee = tx_pool_config.min_fee_rate.fee(true_weight);
if fee < true_min_fee {
    return Err(Reject::LowFeeRate(tx_pool_config.min_fee_rate, true_min_fee.as_u64(), fee.as_u64()));
}
```

This mirrors how `TxEntry::fee_rate()` and `get_transaction_weight` already compute the canonical weight, and closes the gap between the admission check and the true economic cost of including the transaction. [7](#0-6) [4](#0-3) 

## Proof of Concept
1. Deploy a CKB script that consumes close to `max_tx_verify_cycles` (70,000,000) cycles via a tight computation loop.
2. Construct a transaction using that script as the lock, with `tx_size ≈ 200` bytes.
3. Set the transaction fee to `201 shannons` (with default `min_fee_rate = 1000 shannons/KW`).
4. Submit via `send_transaction` RPC.
5. Observe: the transaction is accepted (size-only fee check passes: `201 >= 200`).
6. Compute true fee rate: `201 × 1000 / max(200, 70,000,000 × 0.000_170_571_4) = 201,000 / 11,940 ≈ 16 shannons/KW`.
7. Observe: the true fee rate (~16) is ~62× below `min_fee_rate` (1000), yet the transaction is relayed to peers and occupies pool space until evicted by the size limiter.
8. Repeat continuously to maintain pool pollution and saturate relay bandwidth. [8](#0-7) [2](#0-1)

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

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** tx-pool/src/process.rs (L715-754)
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
        try_or_return_with_snapshot!(ret, submit_snapshot);
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```
