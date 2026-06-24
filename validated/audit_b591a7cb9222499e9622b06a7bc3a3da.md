Audit Report

## Title
Fee-Rate Unit Mismatch in `calculate_min_replace_fee` and `check_tx_fee` Allows RBF Policy Bypass and Low-Fee Pool Admission — (`tx-pool/src/pool.rs`, `tx-pool/src/util.rs`)

## Summary

`FeeRate::fee()` expects a `weight` argument (shannons per kilo-weight, where weight = `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`), but both `calculate_min_replace_fee` and `check_tx_fee` pass raw `tx_size` bytes instead. For a transaction with small serialized size but near-maximum cycle consumption, the computed minimum fee is underestimated by up to ~60×. An unprivileged `send_transaction` RPC caller can exploit this to replace pool transactions while paying a fee increment far below `min_rbf_rate`, and to admit transactions whose effective fee rate is far below `min_fee_rate`.

## Finding Description

**`FeeRate` unit definition** — `FeeRate` is "shannons per kilo-weight": [1](#0-0) 

`FeeRate::fee(weight)` computes `rate × weight / 1000`: [2](#0-1) 

The correct weight is defined by `get_transaction_weight`: [3](#0-2) 

With `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` and `max_tx_verify_cycles = 70,000,000`, the cycle-derived weight is `70,000,000 × 0.000_170_571_4 ≈ 11,940`. A minimal transaction can be ~200 bytes, giving a ~60× discrepancy.

**Root cause 1 — `calculate_min_replace_fee` (RBF path):** The function signature accepts `size: usize` and passes it directly to `min_rbf_rate.fee()`: [4](#0-3) 

The call site passes `entry.size` (not weight): [5](#0-4) 

For a 200-byte, 70M-cycle transaction at `min_rbf_rate = 1500`: correct extra fee = `1500 × 11940 / 1000 = 17,910 shannons`; actual computed = `1500 × 200 / 1000 = 300 shannons` — a 60× underestimate.

**Root cause 2 — `check_tx_fee` (admission path):** The comment explicitly acknowledges the mismatch but treats it as a "cheap check": [6](#0-5) 

Critically, examining `_process_tx` in `process.rs`, `check_tx_fee` is called during `pre_check` (before script execution), and there is **no second fee-rate check** after `verify_rtx` returns the actual cycles: [7](#0-6) 

The `TxEntry` is constructed with the correct cycles at line 751, and `entry.fee_rate()` correctly uses `get_transaction_weight(size, cycles)`: [8](#0-7) 

But this correct fee rate is never checked against `min_fee_rate` or `min_rbf_rate` after admission.

**Default config confirms RBF is enabled:** [9](#0-8) 

## Impact Explanation

**Primary — RBF policy bypass (High):** An attacker who controls the inputs of a transaction in the pool crafts a replacing transaction with small serialized size (~200 bytes) but a RISC-V lock script consuming ~70M cycles. `calculate_min_replace_fee` computes the required fee bump using size-only weight (300 shannons instead of 17,910 shannons). The attacker pays ~1/60th of the intended extra fee, the victim transaction is evicted, and the replacement sits in the pool with an effective fee rate of ~41 shannons/KW — far below `min_fee_rate = 1000` — so miners will not include it. This indefinitely delays the original transaction at minimal cost, constituting a low-cost griefing attack that violates the fee-bump policy. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"** (High, 10001–15000 points).

**Secondary — Pool admission bypass:** Any `send_transaction` caller can submit high-cycles, small-size transactions with fees just above `min_fee_rate.fee(size)`. Each such transaction consumes significant CPU during script verification (70M cycles) while paying a fee far below the intended minimum. This can be used to waste verifier CPU across the network at low cost, compounding the congestion impact.

## Likelihood Explanation

- RBF is enabled by default in the shipped `ckb.toml` (`min_rbf_rate = 1500 > min_fee_rate = 1000`).
- The attack requires only an unprivileged `send_transaction` RPC call — no special role, key, or majority hashpower.
- Crafting a RISC-V program that consumes near-maximum cycles is straightforward; CKB-VM supports arbitrary RISC-V programs.
- The attacker must control the inputs of the transaction being replaced (i.e., they are the sender), which is a realistic scenario for griefing recipients.
- The attack is repeatable: each time the victim resubmits, the attacker can replace again at the same low cost.

## Recommendation

In `calculate_min_replace_fee`, accept `cycles: u64` alongside `size` and compute weight before calling `fee()`:

```rust
fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize, cycles: u64) -> Option<Capacity> {
    let weight = get_transaction_weight(size, cycles);
    let extra_rbf_fee = self.config.min_rbf_rate.fee(weight);
    // ...
}
```

Pass `entry.cycles` at the call site in `check_rbf`.

For `check_tx_fee`, since cycles are not yet known at call time, add a second fee-rate check in `_process_tx` after `verify_rtx` returns the actual cycles, before constructing the `TxEntry` and calling `submit_entry`. Alternatively, pass the cycles from the verify cache entry if available.

## Proof of Concept

1. **Setup:** Node with default config (`min_fee_rate = 1000`, `min_rbf_rate = 1500`, `max_tx_verify_cycles = 70_000_000`).

2. **Submit victim tx1:** Serialized size ≈ 200 bytes, cycles ≈ 1,000, fee = 200 shannons. Effective fee rate = 1000 shannons/KW. Accepted.

3. **Craft attacker tx2:** Same inputs as tx1 (conflict). Lock script is a RISC-V loop consuming 70,000,000 cycles. Serialized size ≈ 200 bytes.
   - `check_tx_fee`: `min_fee = 1000 × 200 / 1000 = 200 shannons`. Pay 501 shannons → passes.
   - `calculate_min_replace_fee`: `extra_rbf_fee = 1500 × 200 / 1000 = 300 shannons`. `min_replace_fee = 200 + 300 = 500 shannons`. Pay 501 shannons → passes RBF check.

4. **Actual weight of tx2:** `max(200, 70,000,000 × 0.000_170_571_4) = 11,940`. Effective fee rate = `501 × 1000 / 11,940 ≈ 41 shannons/KW` — 24× below `min_fee_rate`, 36× below `min_rbf_rate`.

5. **Result:** tx1 is evicted. tx2 occupies the pool at ~41 shannons/KW. Miners sort by fee rate and will not include tx2, indefinitely delaying the original transaction. The correct extra RBF fee should have been `1500 × 11,940 / 1000 = 17,910 shannons`; the attacker paid 300 shannons — a 60× shortfall.

### Citations

**File:** util/types/src/core/fee_rate.rs (L3-7)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);

const KW: u64 = 1000;
```

**File:** util/types/src/core/fee_rate.rs (L33-37)
```rust
    /// Calculates the fee for a given weight.
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

**File:** tx-pool/src/pool.rs (L101-103)
```rust
    /// min_replace_fee = sum(replaced_txs.fee) + extra_rbf_fee
    fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```

**File:** tx-pool/src/pool.rs (L664-666)
```rust
        let fee = entry.fee;
        if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
            if fee < min_replace_fee {
```

**File:** tx-pool/src/util.rs (L42-45)
```rust
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** resource/ckb.toml (L211-215)
```text
max_tx_pool_size = 180_000_000 # 180mb
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
max_tx_verify_cycles = 70_000_000
```
