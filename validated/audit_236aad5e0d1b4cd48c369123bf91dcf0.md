Audit Report

## Title
`check_tx_fee` Uses Size-Only Weight for Minimum Fee Rate Admission, Allowing Cycle-Heavy Transactions to Bypass the Effective Fee Floor — (`tx-pool/src/util.rs`)

## Summary

`check_tx_fee` enforces `min_fee_rate` using only serialized byte size as the transaction weight. The canonical weight formula `get_transaction_weight(size, cycles) = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)` is never applied at admission time because cycles are unknown during `pre_check`. No second fee-rate check is performed after `verify_rtx` determines the actual cycle count, allowing cycle-heavy transactions with a tiny byte footprint to enter the pool at an effective fee rate orders of magnitude below `min_fee_rate`.

## Finding Description

**Root cause — `tx-pool/src/util.rs`, `check_tx_fee`, lines 42–45:**

The function computes the minimum fee using only `tx_size`, and the code comment explicitly acknowledges this is theoretically incorrect:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

**Canonical weight formula — `util/types/src/core/tx_pool.rs`, lines 298–303:**

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

`TxEntry::fee_rate()` uses this formula with both size and cycles, meaning every pool-internal fee-rate consumer applies the correct weight: [3](#0-2) 

**Code flow that creates the gap — `tx-pool/src/process.rs`, `_process_tx`:**

1. `pre_check` calls `check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)` — size-only gate, cycles unknown. [4](#0-3) 

2. `verify_rtx(...)` — actual cycles determined here. [5](#0-4) 

3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry created with real cycles. [6](#0-5) 

4. `submit_entry(tip_hash, entry, status)` — **no second fee-rate check using the now-known cycles**. [7](#0-6) 

There is no `min_fee_rate` or `LowFeeRate` check anywhere between `verify_rtx` and `submit_entry`. The gap is structural and unconditional.

**Concrete numbers (mainnet defaults, `min_fee_rate = 1000 shannons/KW`, `DEFAULT_BYTES_PER_CYCLES ≈ 0.000_170_571_4`):**

- `tx_size = 100 bytes`, `cycles = 70,000,000`
- Size-only min fee (what `check_tx_fee` enforces): `1000 × 100 / 1000 = 100 shannons`
- Correct weight: `max(100, 70_000_000 × 0.000_170_571_4) ≈ 11,940`
- Correct min fee: `1000 × 11,940 / 1000 = 11,940 shannons`
- Effective fee rate of admitted tx: `100 × 1000 / 11,940 ≈ 8 shannons/KW` — **~125× below `min_fee_rate`**

## Impact Explanation

This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker can flood the tx-pool (up to 180 MB by default) with transactions whose effective fee rate is ~125× below `min_fee_rate`. These transactions pass the admission gate, consume pool capacity, persist until expiry (default 12 hours), crowd out legitimate transactions paying the correct fee rate, and consume full script-verification resources (up to `max_tx_verify_cycles` per tx) before the discrepancy is visible. The attacker's cost is proportional to `min_fee_rate × tx_size` (100 shannons per tx), not to the true weight (11,940 shannons), making the attack ~125× cheaper than the damage it causes.

## Likelihood Explanation

- Entry path: standard `send_transaction` RPC, available to any node peer or local caller — no special privilege required.
- A CKB-VM script that loops near `max_tx_verify_cycles` can be written in a small number of RISC-V bytes (tight loop).
- The attacker needs valid UTXOs with the malicious script deployed, requiring on-chain setup but not a meaningful barrier.
- The discrepancy is structural and reproducible on every node running default configuration.
- The code comment at lines 42–44 of `util.rs` explicitly acknowledges the theoretical incorrectness, confirming this is a known design gap without a compensating post-verification check. [8](#0-7) 

## Recommendation

After `verify_rtx` returns the actual cycle count in `_process_tx`, perform a second fee-rate check using the correct weight before calling `submit_entry`:

```rust
// After verify_rtx, cycles are known:
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let effective_min_fee = tx_pool_config.min_fee_rate.fee(actual_weight);
if fee < effective_min_fee {
    return Some((Err(Reject::LowFeeRate(
        tx_pool_config.min_fee_rate,
        effective_min_fee.as_u64(),
        fee.as_u64(),
    )), snapshot));
}
```

This mirrors the pattern already used in `TxEntry::fee_rate()` and all pool-internal fee-rate consumers. [9](#0-8) 

## Proof of Concept

```
1. Deploy a CKB lock/type script that executes a tight loop consuming
   ~70,000,000 cycles. The compiled RISC-V bytecode for such a loop
   is on the order of tens of bytes.

2. Craft a transaction with:
   - tx_size ≈ 100 bytes (minimal inputs/outputs referencing the script cell)
   - fee = 100 shannons  (= min_fee_rate × tx_size / 1000 = 1000 × 100 / 1000)

3. Submit via send_transaction RPC.

4. Observe: transaction is accepted (check_tx_fee passes: 100 >= 100 shannons).

5. Query pool entry via get_pool_tx_detail_info:
   - entry.fee_rate() = fee * 1000 / get_transaction_weight(100, 70_000_000)
                      = 100 * 1000 / 11940 ≈ 8 shannons/KW

6. Repeat with many UTXOs to fill the 180 MB pool with ~8 shannons/KW entries,
   blocking legitimate 1000 shannons/KW transactions from entering.
```

The structural gap is confirmed by the code comment itself acknowledging the theoretical incorrectness of the size-only check, combined with the confirmed absence of any post-verification fee-rate enforcement between `verify_rtx` and `submit_entry` in `_process_tx`. [1](#0-0) [10](#0-9)

### Citations

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
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

**File:** tx-pool/src/process.rs (L287-290)
```rust
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
```

**File:** tx-pool/src/process.rs (L724-754)
```rust
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
