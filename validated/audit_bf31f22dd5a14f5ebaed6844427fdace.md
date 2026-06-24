Audit Report

## Title
Fee Rate Admission Check Uses Raw Transaction Size Instead of Actual Weight, Allowing Cycle-Heavy Transactions to Bypass `min_fee_rate` - (File: tx-pool/src/util.rs)

## Summary
`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using only `tx_size` (serialized bytes), while the canonical weight used everywhere else in the system is `get_transaction_weight(tx_size, cycles) = max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For cycle-heavy transactions these two values diverge by orders of magnitude. No second fee-rate check is performed after `verify_rtx` determines the actual cycle count, so under-priced cycle-heavy transactions are permanently admitted to the pool after forcing full script execution.

## Finding Description
`check_tx_fee` is the sole fee-rate gate for pool admission. The code itself acknowledges the limitation with an explicit comment:

```rust
// tx-pool/src/util.rs  lines 42-52
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

`FeeRate::fee(weight)` computes `fee_rate * weight / 1000` (shannons per kilo-weight):

```rust
// util/types/src/core/fee_rate.rs  lines 34-37
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;
    Capacity::shannons(fee)
}
```

The canonical weight function is:

```rust
// util/types/src/core/tx_pool.rs  lines 298-303
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64,
                  (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

In `_process_tx`, after `verify_rtx` returns the actual cycle count at line 734, the code immediately constructs a `TxEntry` and calls `submit_entry` with no intervening fee-rate check:

```rust
// tx-pool/src/process.rs  lines 734, 751-753
let verified = try_or_return_with_snapshot!(verified_ret, snapshot);
// ... no fee-rate re-check here ...
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
```

The pool's ordering and eviction logic (`AncestorsScoreSortKey`, `EvictKey`) do use `get_transaction_weight(size, cycles)`, but those mechanisms only evict when the pool is full — they do not reject an already-admitted entry and do not undo the script execution cost already paid by the node.

**Concrete example with `min_fee_rate = 1000` shannons/KW:**

| Parameter | Value |
|---|---|
| `tx_size` | 100 bytes |
| `cycles` | 10,000,000 |
| Actual weight | `max(100, 10,000,000 × 0.000_170_571_4)` = **1,705** |
| Actual min fee | `1000 × 1705 / 1000` = **1,705 shannons** |
| Check min fee | `1000 × 100 / 1000` = **100 shannons** |

A transaction paying 101 shannons passes `check_tx_fee` but has an effective fee rate of `101 × 1000 / 1705 ≈ 59 shannons/KW` — 17× below the configured minimum.

## Impact Explanation
This is a **High** severity finding matching the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

An unprivileged transaction sender can permanently admit transactions to the mempool whose true fee rate is far below `min_fee_rate`. Each such transaction forces the node to execute full script verification (proportional to `cycles`) before the under-payment is detectable, occupies pool memory until eviction, and consumes CPU during pool-ordering operations. Because the attacker pays only the size-proportional fee, the cost-to-impact ratio is highly asymmetric for cycle-heavy scripts. Repeated submission exhausts verification worker capacity and pool memory.

## Likelihood Explanation
The attack is straightforward and requires no special privilege, key material, or majority hash power. Any user with a valid UTXO can construct a transaction with a computationally expensive lock script and a small serialized size. Both the `send_transaction` RPC endpoint and the P2P relay path funnel through `pre_check` → `check_tx_fee`, making the attack reachable from any unprivileged caller. The attacker can pre-fund many small UTXOs to sustain a prolonged attack.

## Recommendation
After `verify_rtx` returns the actual cycle count in `_process_tx`, perform a second fee-rate check using the true weight before calling `submit_entry`:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let actual_min_fee = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < actual_min_fee {
    return Some((Err(Reject::LowFeeRate(
        tx_pool.config.min_fee_rate,
        actual_min_fee.as_u64(),
        fee.as_u64(),
    )), snapshot));
}
```

This mirrors the pattern used in pool ordering and eviction and closes the gap between the admission check and the rest of the system.

## Proof of Concept
1. Construct a CKB transaction with a lock script that consumes ~10,000,000 cycles but whose serialized size is ~100 bytes.
2. Set the output capacity so that `inputs_capacity − outputs_capacity = 101 shannons` (fee = 101 shannons).
3. Submit via `send_transaction` RPC to a node with `min_fee_rate = 1000`.
4. `check_tx_fee` computes `min_fee = 1000 × 100 / 1000 = 100 shannons`; fee 101 > 100 → **admitted**.
5. `verify_rtx` executes the script, consuming ~10,000,000 cycles.
6. No second fee-rate check is performed; the transaction is submitted to the pool.
7. The actual weight is 1,705; the effective fee rate is ≈ 59 shannons/KW, well below the 1,000 shannons/KW minimum.
8. Repeat with many such transactions (pre-funded UTXOs) to exhaust pool memory and verification worker capacity.