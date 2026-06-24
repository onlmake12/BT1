Audit Report

## Title
Single-Dimension Fee Rate Admission Check Ignores Cycle Weight, Allowing Below-Minimum Fee Rate Transactions Into the Pool - (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces `min_fee_rate` using only serialized transaction size, while the canonical two-dimensional weight `max(size, cycles × DEFAULT_BYTES_PER_CYCLES)` used everywhere else in the pool is never applied at admission. No post-verification fee rate re-check using actual cycles exists. A cycle-heavy transaction can pass the admission gate paying only the size-floor fee, resulting in an effective fee rate up to ~60× below the configured `min_fee_rate`.

## Finding Description

`check_tx_fee` computes the minimum required fee using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The canonical two-dimensional weight function used everywhere else is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
``` [2](#0-1) 

`TxEntry::fee_rate()` and eviction scoring both use this two-dimensional weight: [3](#0-2) 

In `_process_tx`, after `verify_rtx` returns actual cycles, the code only checks whether declared cycles match actual cycles (for remote peers), then creates the `TxEntry` and calls `submit_entry` — there is no fee rate re-check against the cycle-adjusted weight: [4](#0-3) 

The eviction trigger is byte-size only:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
``` [5](#0-4) 

A 200-byte transaction consuming 70,000,000 cycles has a cycle-adjusted weight of `max(200, 70_000_000 × 0.000_170_571) ≈ 11,940`. `check_tx_fee` requires only `1000 × 200 / 1000 = 200` shannons, while the true minimum by canonical weight would be `1000 × 11,940 / 1000 = 11,940` shannons — approximately 60× below `min_fee_rate`.

## Impact Explanation

This matches the **High** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

An attacker paying only the size-floor fee (200 shannons per transaction) consumes the maximum allowed verification cycles (70M) per transaction. Because `total_tx_size` is the only eviction trigger and these transactions are tiny (200 bytes), they do not trigger byte-based eviction and persist in the pool. The attacker achieves ~60× leverage on verification resource consumption relative to the fee paid, saturating verification workers and delaying or starving legitimate high-fee transactions. [6](#0-5) 

## Likelihood Explanation

The attack requires only an RPC call to `send_transaction` — no privileged access, no key material, no majority hashpower. The attacker must deploy a script that genuinely consumes close to `max_tx_verify_cycles` cycles (a tight loop in CKB-VM), which is straightforward for any script author, and hold UTXOs locked by that script (a one-time setup cost). The discrepancy grows linearly with cycles, so the attacker naturally targets the maximum cycle budget. The default `DEFAULT_MAX_TX_VERIFY_CYCLES = TWO_IN_TWO_OUT_CYCLES * 20` is large enough to produce the ~60× weight gap. [7](#0-6) 

## Recommendation

Replace the size-only weight in `check_tx_fee` with the two-dimensional weight. For the pre-verification admission check, use the declared cycle count (available from the `remote` parameter for P2P submissions, or `max_block_cycles` as a conservative bound for local RPC submissions):

```rust
let weight = get_transaction_weight(tx_size, declared_cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

After script execution, add a post-verification re-check using the actual verified cycles to close the remaining gap between declared and actual cycles. This mirrors the existing `DeclaredWrongCycles` check pattern already present in `_process_tx`. [8](#0-7) 

## Proof of Concept

1. Author a CKB lock script that runs a tight loop consuming exactly `max_tx_verify_cycles - 1` cycles. Deploy it on testnet.
2. Create cells locked by that script.
3. Construct a transaction spending one such cell. Serialized size ≈ 200 bytes.
4. Set fee to `ceil(min_fee_rate × tx_size / 1000)` = 200 shannons.
5. Submit via `send_transaction` RPC. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200` shannons; the transaction passes admission.
6. After verification the pool records `fee_rate ≈ 16 shannons/KB` — 60× below `min_fee_rate`.
7. Repeat with many UTXOs. Each transaction occupies only 200 bytes of `total_tx_size` (never triggering byte-based eviction) while consuming 70M cycles of verification work per entry, saturating the verification worker pool and delaying legitimate high-fee transactions. [9](#0-8)

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

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/process.rs (L734-753)
```rust
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

**File:** tx-pool/src/pool.rs (L298-298)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
```

**File:** util/app-config/src/legacy/tx_pool.rs (L14-14)
```rust
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
```
