Audit Report

## Title
`check_tx_fee` Uses `tx_size`-Only Weight for Admission Gate, Allowing Cycle-Heavy Transactions to Bypass Minimum Fee Rate — (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` computes the minimum required fee using only the serialized transaction size, while the pool's internal ordering and eviction use `get_transaction_weight(size, cycles) = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. A cycle-heavy, byte-light transaction can pass the admission gate with a fee far below what its actual weight warrants, enabling mempool flooding at a fraction of the intended cost. The same flaw exists in `calculate_min_replace_fee` for RBF.

## Finding Description

In `tx-pool/src/util.rs` at L45, `check_tx_fee` computes:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code comment at L42–44 explicitly acknowledges this is intentional ("here min fee rate is used as a cheap check"), but no second fee check using verified cycles is performed after `verify_rtx` returns the actual cycle count. The call site in `tx-pool/src/process.rs` at L289 and L294 shows `check_tx_fee` is called inside `pre_check`, before script verification. After `verify_rtx` completes and cycles are known, `submit_entry` is called with no cycle-aware fee re-validation.

In contrast, `TxEntry::fee_rate()` at `tx-pool/src/component/entry.rs` L116 correctly uses:

```rust
let weight = get_transaction_weight(self.size, self.cycles);
FeeRate::calculate(self.fee, weight)
```

And `get_transaction_weight` at `util/types/src/core/tx_pool.rs` L298–303 is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

The admission gate and the pool's internal fee-rate accounting are therefore inconsistent. A transaction admitted via the size-only check can have an effective fee rate (as computed by `fee_rate()`) far below `min_fee_rate`.

`calculate_min_replace_fee` in `tx-pool/src/pool.rs` L103 has the same flaw:

```rust
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```

## Impact Explanation

With `max_tx_verify_cycles = 70,000,000` and `DEFAULT_BYTES_PER_CYCLES ≈ 0.000_170_571_4`:

- A transaction with `tx_size = 100` bytes and `cycles = 70,000,000` has `actual_weight = max(100, 11,940) = 11,940`.
- Admission requires `min_fee_rate × 100 / 1000 = 100 shannons` (at default 1000 shannons/KW).
- Correct weight-based minimum: `1000 × 11,940 / 1000 = 11,940 shannons`.
- Discrepancy: ~119×.

An attacker can flood the mempool with cycle-heavy, byte-light transactions at ~1/119th of the intended minimum cost. Each admitted transaction: (1) consumes up to 70M cycles of script verification CPU on every node, (2) occupies a pool slot with an effective fee rate of ~8 shannons/KW against a 1000 shannons/KW minimum, and (3) displaces legitimate transactions when the pool fills and eviction runs. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation

Any unprivileged user reachable via `send_transaction` JSON-RPC or P2P relay can exploit this. Crafting a cycle-heavy, byte-light transaction requires only writing a lock script that loops for ~70M cycles but whose serialized bytecode is small — a tight loop in CKB-VM achieves this trivially. No special privileges, keys, or majority hashpower are required. The attack is repeatable across many UTXOs.

## Recommendation

1. After `verify_rtx` returns the verified `Completed` (which contains the actual cycle count), perform a second fee check using `get_transaction_weight(tx_size, verified_cycles)` before calling `submit_entry`. This is the most accurate fix.
2. Alternatively, use `get_transaction_weight(tx_size, declared_cycles)` in `check_tx_fee` as a pre-verification gate, where `declared_cycles` is the caller-declared cycle limit already available in the submission path.
3. Apply the same fix to `calculate_min_replace_fee`: replace `self.config.min_rbf_rate.fee(size as u64)` with a weight-based calculation using the replacement transaction's cycles.

## Proof of Concept

1. Write a CKB lock script that executes a tight loop consuming ~70,000,000 cycles; keep the serialized script bytecode small (~50–80 bytes). The total transaction serialized size will be ~100 bytes.
2. Set the transaction fee to `101 shannons` (just above `min_fee_rate × 100 / 1000 = 100` at default 1000 shannons/KW).
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 100 shannons`; the transaction passes admission.
5. After verification, `TxEntry::fee_rate()` computes `FeeRate::calculate(101, 11940) ≈ 8 shannons/KW` — far below the 1000 shannons/KW minimum.
6. Repeat across many UTXOs to fill the mempool with ~8 shannons/KW transactions at ~101 shannons each instead of the intended ~11,940 shannons each, while each submission forces every peer node to execute 70M cycles of script verification. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** tx-pool/src/pool.rs (L101-103)
```rust
    /// min_replace_fee = sum(replaced_txs.fee) + extra_rbf_fee
    fn calculate_min_replace_fee(&self, conflicts: &[&PoolEntry], size: usize) -> Option<Capacity> {
        let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```

**File:** tx-pool/src/process.rs (L287-295)
```rust
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
```
