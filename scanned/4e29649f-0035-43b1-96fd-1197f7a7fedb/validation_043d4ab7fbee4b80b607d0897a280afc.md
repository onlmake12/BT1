Audit Report

## Title
`check_tx_fee` Uses `tx_size` as Weight Instead of Composite Transaction Weight, Allowing CPU-Heavy Transactions to Bypass Minimum Fee Rate Admission — (`tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate gate using only the serialized byte size of the transaction (`tx_size`) as the weight denominator, rather than the correct composite weight `get_transaction_weight(tx_size, cycles)`. After `verify_rtx` returns the actual cycles, no second fee rate check is performed using the proper weight. An attacker can craft a CPU-heavy transaction with a small serialized body, pay a fee just above the size-only minimum, pass the gate, force full script verification, and be admitted to the tx-pool — all at a fraction of the correct minimum fee cost.

## Finding Description

**Root cause:** `check_tx_fee` computes `min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64)` (line 45 of `tx-pool/src/util.rs`). The code comment explicitly acknowledges this is a deliberate approximation: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check."* However, the correct weight formula is `get_transaction_weight(tx_size, cycles) = max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)` (defined in `util/types/src/core/tx_pool.rs:298-303`, with `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`).

**Missing post-verification check:** In `_process_tx` (`tx-pool/src/process.rs`), the flow is:
1. `pre_check` → calls `check_tx_fee` with `tx_size` only (line 289)
2. `verify_rtx` → returns actual `verified.cycles` (line 724-734)
3. `DeclaredWrongCycles` check (line 736-748) — only validates declared vs actual cycles, not fee rate
4. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` (line 751) — entry created and submitted

There is no step between 2 and 4 that re-evaluates the fee rate using the now-known `verified.cycles`. The entry's `fee_rate()` method in `tx-pool/src/component/entry.rs:115-118` does use `get_transaction_weight(self.size, self.cycles)` correctly, but this is only used for sorting and eviction priority — not for admission gating.

**Exploit flow:**
- Attacker crafts a transaction: serialized size ≈ 200 bytes, cycles ≈ 70,000,000
- At `min_fee_rate = 1000 shannons/KB`:
  - Size-only `min_fee = 1000 * 200 / 1000 = 200 shannons` → attacker pays 201 shannons → passes `check_tx_fee`
  - Proper weight = `max(200, 70,000,000 × 0.000_170_571_4) ≈ 11,940`
  - Proper `min_fee = 1000 * 11,940 / 1000 = 11,940 shannons` → attacker underpays by ~59×
- `verify_rtx` runs full 70M-cycle script verification — CPU cost already paid by the node
- `DeclaredWrongCycles` check passes (attacker knows their own script's cycle count)
- Transaction admitted to tx-pool

**Existing guards are insufficient:**
- The `DeclaredWrongCycles` check only enforces cycle declaration accuracy, not fee adequacy
- Pool eviction (`limit_size`) uses proper weight-based fee rate but only triggers after admission and only when the pool is full
- The tx-pool size limit does not prevent the CPU cost of verification from being incurred

## Impact Explanation

This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker can repeatedly submit CPU-heavy transactions at ~59× below the correct minimum fee, forcing each target node to execute full script verification (up to `max_tx_verify_cycles = 70,000,000` cycles per transaction) before the transaction is admitted. The attacker's cost per transaction is 201 shannons; the correct cost should be 11,940 shannons. At scale, this exhausts node CPU resources and fills the tx-pool with low-effective-fee-rate entries, displacing legitimate transactions and degrading relay performance across the network.

## Likelihood Explanation

Any unprivileged RPC caller (`send_transaction`) or P2P relay peer can trigger this. No special privileges are required. Crafting a script that consumes a predictable number of cycles but serializes to a small witness is straightforward — a tight loop in CKB-VM is sufficient. The attacker knows the exact cycle count because they authored the script, so the `DeclaredWrongCycles` check is trivially satisfied. The attack is repeatable with minimal on-chain cost.

## Recommendation

After `verify_rtx` returns `verified.cycles` and before `submit_entry`, perform a second fee rate check using the proper composite weight:

```rust
// In _process_tx, after verify_rtx and DeclaredWrongCycles check:
let proper_weight = get_transaction_weight(tx_size, verified.cycles);
let proper_min_fee = tx_pool_config.min_fee_rate.fee(proper_weight);
if fee < proper_min_fee {
    return Some((
        Err(Reject::LowFeeRate(tx_pool_config.min_fee_rate, proper_min_fee.as_u64(), fee.as_u64())),
        snapshot,
    ));
}
```

This mirrors the correct pattern already used in `TxEntry::fee_rate()` (`tx-pool/src/component/entry.rs:115-118`) and `FeeRateCollector::statistics()` (`rpc/src/util/fee_rate.rs:103-105`). The pre-verification size-only check can remain as a cheap early filter; the post-verification check closes the admission gap.

## Proof of Concept

1. Configure a CKB node with default `min_fee_rate = 1000 shannons/KB`.
2. Write a CKB lock script that executes a tight loop consuming exactly 70,000,000 cycles but whose witness serializes to ≈200 bytes.
3. Create a cell locked by this script on a dev chain.
4. Submit a transaction spending that cell via `send_transaction` RPC, paying a fee of 201 shannons (above `min_fee_rate.fee(200) = 200`).
5. Observe: `check_tx_fee` passes (201 > 200); `verify_rtx` runs full 70M-cycle verification; transaction is admitted to the tx-pool.
6. Verify the admitted entry's effective fee rate: `201 * 1000 / 11940 ≈ 16 shannons/KB` — far below the 1000 shannons/KB minimum.
7. Repeat in a loop to exhaust node CPU and fill the tx-pool with sub-minimum-fee-rate entries.

For P2P relay: declare the correct cycle count (70,000,000) alongside the transaction; the `DeclaredWrongCycles` check passes, and the same admission occurs.