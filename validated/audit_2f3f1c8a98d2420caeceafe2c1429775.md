All code claims verified against the actual repository. Here is the validation result:

---

Audit Report

## Title
Size-Only Admission Fee Check Allows Cycle-Heavy Transactions to Bypass Effective Minimum Fee Rate — (File: tx-pool/src/util.rs)

## Summary
`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the serialized byte size of a transaction. The true weight used for sorting and eviction is `get_transaction_weight(tx_size, cycles) = max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. For a cycle-heavy, size-light transaction at default `max_tx_verify_cycles = 70,000,000`, the weight diverges from size by ~79×, allowing an attacker to trigger full script verification at a fraction of the intended minimum cost, enabling sustained CPU exhaustion and mempool pressure.

## Finding Description
**Admission gate — size only:**

`check_tx_fee` is called inside `pre_check` at `process.rs` lines 289 and 294, before `verify_rtx` runs and before cycles are known:

```rust
// tx-pool/src/util.rs:42-45
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The comment explicitly acknowledges the mismatch.

**No post-verification weight check:**

After `verify_rtx` returns `verified.cycles` at `process.rs` line 734, `TxEntry` is constructed and submitted with no additional fee check:

```rust
// tx-pool/src/process.rs:751-753
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
```

**True weight used everywhere inside the pool:**

`TxEntry::fee_rate()` and `AncestorsScoreSortKey` both use `get_transaction_weight(size, cycles)`:

```rust
// tx-pool/src/component/entry.rs:115-118
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

```rust
// util/types/src/core/tx_pool.rs:298-303
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
// DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4
```

**Numerical gap at default settings (`min_fee_rate = 1000 shannons/KW`, `max_tx_verify_cycles = 70,000,000`):**

| Metric | Value |
|---|---|
| tx_size | 150 bytes |
| Admission min fee (size-only) | 150 shannons |
| True weight | max(150, 70,000,000 × 0.000_170_571_4) ≈ 11,940 |
| Effective fee rate by true weight | 150 × 1000 / 11,940 ≈ 12 shannons/KW |
| Configured min_fee_rate | 1,000 shannons/KW |
| **Underpayment ratio** | **~79×** |

**Why existing checks fail:**

The `EvictKey` does use the true weight-based fee rate, so the attacker's transactions rank lowest for eviction. However, this does not prevent the attack — it only means the attacker's transactions are evicted first when the pool is full. The attacker can continuously resubmit, forcing the node to re-run full script verification (up to 70M cycles per transaction) on each submission. The admission gate never rejects them because the size-based fee check always passes.

## Impact Explanation
**High — "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

1. **CPU exhaustion**: Every submitted transaction triggers full contextual script verification (`verify_rtx`, up to 70M cycles). At ~79× reduced admission cost, an attacker can sustain a verification workload ~79× larger than the fee floor was designed to permit.
2. **Mempool churn**: Continuous submission/eviction cycles consume lock contention, pool bookkeeping, and P2P relay bandwidth.
3. **Block template degradation**: The attacker's transactions rank at the bottom of `AncestorsScoreSortKey` and are never mined, yet they occupy pool slots and consume miner block-assembly CPU during template construction.

## Likelihood Explanation
- Reachable via the public `send_transaction` RPC and P2P relay — no privilege required.
- Requires only a lock script that loops to consume ~70M cycles while serializing to ~100–200 bytes. Standard RISC-V tight-loop scripts satisfy this.
- At `min_fee_rate = 1000 shannons/KW`, the attacker pays ~150 shannons per transaction instead of ~11,940 shannons — a ~79× cost reduction per verification event imposed on the node.
- The attack is repeatable and stateless; the attacker need not maintain any persistent state beyond a funded cell.

## Recommendation
Add a post-verification fee check using the true weight immediately after `verify_rtx` returns, before `TxEntry` is constructed at `tx-pool/src/process.rs` line 751:

```rust
// tx-pool/src/process.rs — after line 734
let true_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(true_weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(
        tx_pool_config.min_fee_rate,
        min_fee_by_weight.as_u64(),
        fee.as_u64(),
    )), snapshot));
}
```

The size-only check in `check_tx_fee` can remain as a fast pre-filter, but the weight-based check must be enforced once cycles are known.

## Proof of Concept
**Setup**: default mainnet config, `min_fee_rate = 1000 shannons/KW`, `max_tx_verify_cycles = 70,000,000`.

**Step 1 — Craft the transaction:**
- 1 input cell, 1 output cell → serialized size ≈ 150 bytes.
- Lock script: a tight RISC-V loop consuming ~70,000,000 cycles, compiled to a small binary stored in a dep cell.
- Fee = `1000 × 150 / 1000 = 150 shannons` (exactly the size-based minimum).

**Step 2 — Submit via RPC:**
```json
{"method": "send_transaction", "params": [<crafted_tx>, "passthrough"]}
```

**Step 3 — Observe:**
- `check_tx_fee` passes: `fee(150) = 150 >= 150`.
- `verify_rtx` runs the script, consuming 70M cycles of node CPU.
- No post-verification weight check exists; entry is inserted.
- True fee rate: `150 × 1000 / 11,940 ≈ 12 shannons/KW` — ~79× below `min_fee_rate`.
- Entry is immediately eviction-eligible, but the attacker resubmits continuously.

**Step 4 — Sustained attack:**
- Each submission forces a full 70M-cycle script execution on the node.
- Node CPU is saturated; legitimate transaction verification is delayed.

**Verification test plan**: Write a unit test in `tx-pool/src/component/tests/` that creates a `TxEntry` with `size = 150`, `cycles = 70_000_000`, `fee = 150 shannons`, and asserts that `entry.fee_rate() < min_fee_rate` — confirming the admitted entry violates the configured floor.