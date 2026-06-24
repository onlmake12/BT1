Audit Report

## Title
`check_tx_fee` Uses `tx_size` Instead of Weight for `min_fee_rate` Enforcement, Allowing High-Cycles Transactions to Bypass the Fee Floor — (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` enforces the pool's minimum fee rate using only the serialized byte size of the transaction, while the canonical fee-rate unit is shannons per kilo-weight where `weight = max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For a transaction whose cycle cost dominates its byte size, the size-only check admits the transaction at a fee far below `min_fee_rate`. No weight-based fee-rate check is performed after `verify_rtx` returns the actual cycle count before the entry is inserted into the pool. An attacker can force the entire network to spend significant CPU time verifying high-cycle transactions at approximately 5% of the intended minimum cost.

## Finding Description

**Root cause — `check_tx_fee` (`tx-pool/src/util.rs` L42–52):**

The function explicitly uses `tx_size` as the weight argument to `FeeRate::fee`, with a comment acknowledging the limitation:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

**No second check after cycles are known — `_process_tx` (`tx-pool/src/process.rs` L724–753):**

After `verify_rtx` returns the actual cycle count, the code creates the `TxEntry` with real cycles and immediately calls `submit_entry` with no intervening weight-based fee-rate check:

```rust
let verified = try_or_return_with_snapshot!(verified_ret, snapshot);
// ... declared_cycles mismatch check only ...
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
```

**True weight formula — `util/types/src/core/tx_pool.rs` L298–303:**

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`.

**`TxEntry::fee_rate()` correctly uses weight (`tx-pool/src/component/entry.rs` L115–118):**

```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

This means the entry is inserted with a true weight-based fee rate far below `min_fee_rate`, but the gate check never caught it.

**Eviction uses weight-based fee rate (`tx-pool/src/component/entry.rs` L234–247, `tx-pool/src/component/sort_key.rs` L80–103):**

The `EvictKey` correctly computes fee rate using `get_transaction_weight`, so `limit_size` will eventually evict these entries first — but only after they have already consumed CPU verification time and pool capacity.

**`calculate_min_replace_fee` has the same pattern (`tx-pool/src/pool.rs` L103):**

```rust
let extra_rbf_fee = self.config.min_rbf_rate.fee(size as u64);
```

The RBF extra-fee floor also uses raw size instead of weight.

## Impact Explanation

**Concrete numbers (mainnet defaults):**

| Parameter | Value |
|---|---|
| `min_fee_rate` | 1,000 shannons/kilo-weight |
| Typical tx `tx_size` | 597 bytes |
| `max_tx_verify_cycles` | 70,000,000 |
| Cycle-equivalent bytes | 70,000,000 × 0.000_170_571_4 ≈ 11,940 |
| True `weight` | max(597, 11,940) = **11,940** |
| Size-based min fee required | 597 shannons |
| Weight-based min fee required | 11,940 shannons |
| True fee rate of admitted entry | 597 × 1,000 / 11,940 ≈ **50 shannons/kilo-weight** |

The attacker pays ~5% of the intended minimum fee. Each submitted transaction forces every receiving node (via relay) to execute 70M cycles of script verification. This matches the **High** impact class: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs." The CPU amplification across the relay network is the primary impact — the pool eviction mechanism (`limit_size`) does eventually remove these entries, but only after the verification cost has already been paid by every node that received the transaction.

## Likelihood Explanation

The attack path is fully unprivileged. Any RPC caller (`send_transaction`) or network peer (relay with declared high cycles) can submit the transaction. Crafting a CKB RISC-V lock script that consumes near-maximum cycles (tight loop) while keeping the serialized transaction small is straightforward and requires no special keys, majority hashpower, or social engineering. The attack is repeatable at scale: the attacker continuously submits new transactions, each forcing network-wide verification at 5% of the intended cost.

## Recommendation

After `verify_rtx` returns the actual cycle count, perform a second fee-rate check using the true weight before creating and inserting the `TxEntry` in `_process_tx` (`tx-pool/src/process.rs`):

```rust
// After verify_rtx returns `verified.cycles`:
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(
        tx_pool_config.min_fee_rate, min_fee_by_weight.as_u64(), fee.as_u64()
    )), snapshot));
}
```

Apply the same fix to `calculate_min_replace_fee` in `tx-pool/src/pool.rs` L103, substituting the entry's true weight (using `entry.cycles`) for the raw `size`.

## Proof of Concept

1. Write a CKB lock script that executes a tight loop consuming ~70,000,000 cycles. Deploy it on-chain.
2. Construct a transaction spending a cell locked by that script. Serialized size ≈ 597 bytes.
3. Set the transaction fee to 597 shannons (= `min_fee_rate × tx_size / 1000`).
4. Submit via `send_transaction` RPC.
5. `check_tx_fee` (`tx-pool/src/util.rs` L45) computes `min_fee = 1000 × 597 / 1000 = 597` — check passes.
6. `verify_rtx` executes the script, consuming ~70,000,000 cycles (CPU cost paid by node).
7. `TxEntry` is created with `cycles ≈ 70,000,000`, `fee = 597`, `size = 597` — no second fee-rate check.
8. `entry.fee_rate()` = `597 × 1000 / 11,940 ≈ 50 shannons/kilo-weight` — 20× below `min_fee_rate`.
9. Entry is inserted into the pool; `limit_size` eventually evicts it (lowest weight-based fee rate), but verification CPU cost is already spent.
10. Transaction is relayed to peers; each peer also spends 70M cycles verifying it at the same 597-shannon cost to the attacker.
11. Repeat to continuously force network-wide CPU expenditure at 5% of the intended minimum cost.