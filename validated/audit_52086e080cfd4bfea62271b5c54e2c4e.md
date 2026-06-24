Audit Report

## Title
`check_tx_fee` Uses Serialized Byte Size Instead of Weight for Minimum Fee Enforcement, Enabling Compute-Heavy DoS - (File: `tx-pool/src/util.rs`)

## Summary
`check_tx_fee` computes the minimum required fee by passing raw serialized byte size (`tx_size`) directly to `FeeRate::fee()`, which is defined as shannons per kilo-weight. For compute-heavy transactions, the true weight (dominated by cycles) can be up to ~60× larger than the byte size, meaning the admission fee gate is proportionally weaker. An unprivileged RPC caller can submit transactions that pass the fee check while paying far less than `min_fee_rate` actually requires, forcing the node to execute expensive CKB-VM scripts at a fraction of the intended cost.

## Finding Description

`FeeRate` is explicitly defined as **shannons per kilo-weight** (`shannons/KW`):

```rust
// util/types/src/core/fee_rate.rs, line 3
/// shannons per kilo-weight
pub struct FeeRate(pub u64);
```

`FeeRate::fee(weight)` computes `fee_rate × weight / 1000`:

```rust
pub fn fee(self, weight: u64) -> Capacity {
    let fee = self.0.saturating_mul(weight) / KW;
    Capacity::shannons(fee)
}
```

The correct weight formula accounts for both size and cycles:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`.

However, in `check_tx_fee` (called from `pre_check` before any script execution), the minimum fee is computed using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code comment acknowledges the unit mismatch but treats it as an acceptable approximation. It is not acceptable from a security standpoint.

The full transaction processing flow in `_process_tx` is:
1. `pre_check` → `check_tx_fee` (uses `tx_size` only, cycles unknown) → passes
2. `verify_rtx` → full CKB-VM script execution → `verified.cycles` now known
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` → entry created with true cycles
4. **No post-verification fee-rate re-check against true weight**

After verification, `verified.cycles` is available but is never used to re-validate the fee rate against `min_fee_rate`. The pool's internal ordering (`fee_rate()`, `AncestorsScoreSortKey`) correctly uses `get_transaction_weight(size, cycles)`, but the admission gate does not.

For a transaction with 200 serialized bytes and `max_tx_verify_cycles ≈ 70,000,000` cycles (default `TWO_IN_TWO_OUT_CYCLES × 20`):

| Quantity | Value |
|---|---|
| `tx_size` (bytes) | 200 |
| True weight | ≈ 11,940 |
| `min_fee` charged by gate | `1000 × 200 / 1000 = 200 shannons` |
| Fee actually required | `1000 × 11,940 / 1000 = 11,940 shannons` |
| Discount factor | **~60×** |

## Impact Explanation

This matches the **High** impact category: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

An attacker can saturate the node's script verification worker pool (default: 3/4 of CPU cores) by submitting many compute-heavy transactions that each consume up to `max_tx_verify_cycles` cycles while paying only the size-based minimum fee — approximately 60× less than the node operator intends. Because the damage occurs during the verification phase (before eviction can act), `max_tx_pool_size` and eviction logic do not prevent it. If multiple nodes are targeted simultaneously, this constitutes network-wide congestion at minimal economic cost to the attacker.

## Likelihood Explanation

- Requires only a valid RPC connection — no privileged access, no key material, no majority hashpower.
- Crafting a script that loops for ~70M cycles with a small serialized size is straightforward for any CKB script author.
- The fee cost to the attacker is ~60× lower than the node operator intends, making sustained attacks economically viable.
- The default `max_tx_verify_workers = 3/4 × CPU cores` amplifies the per-transaction impact.
- The attack is repeatable and parallelizable.

## Recommendation

Add a post-verification fee-rate re-check in `_process_tx` after `verify_rtx` returns the true cycle count:

```rust
let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

// Re-check fee rate with true weight now that cycles are known
let true_weight = get_transaction_weight(tx_size, verified.cycles);
let true_min_fee = tx_pool_config.min_fee_rate.fee(true_weight);
if fee < true_min_fee {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

Alternatively, replace the pre-check with a weight-based check using a conservative cycle estimate (e.g., `max_tx_verify_cycles` as an upper bound), or enforce both a per-byte and a per-weight minimum. At minimum, the post-verification re-check must be added to close the admission gap.

## Proof of Concept

1. Construct a CKB transaction whose lock or type script loops for ~70,000,000 cycles but whose serialized size is ~200 bytes.
2. Set the transaction fee to `ceil(1000 × 200 / 1000) = 200 shannons` (at default `min_fee_rate = 1000`).
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200 shannons` → **passes**.
5. The node proceeds to full CKB-VM verification, executing ~70M cycles per worker thread.
6. The transaction is admitted with an actual fee rate of `1000 × 200 / 11940 ≈ 16 shannons/KW` — far below `min_fee_rate = 1000`.
7. Repeat with many concurrent transactions to exhaust the verification worker pool.
8. Confirm: node's verification workers are saturated; legitimate transactions experience severe delays.