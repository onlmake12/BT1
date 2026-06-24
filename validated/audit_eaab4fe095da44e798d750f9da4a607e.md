Audit Report

## Title
Tx-Pool Minimum Fee Check Uses Serialized Size Instead of Weight, Allowing Cycle-Heavy Transactions to Bypass Fair Fee Accounting - (File: tx-pool/src/util.rs)

## Summary
The `check_tx_fee` function in `tx-pool/src/util.rs` computes the minimum required fee using only the transaction's serialized byte size (`tx_size`), while the actual block resource consumed is determined by `weight = max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`. Because cycles are only known after script execution (which occurs after the fee check), a transaction sender can submit a compact transaction whose script consumes a large portion of the block cycle budget while paying fees proportional only to its small byte footprint. No second fee check is performed after cycles are known.

## Finding Description
The code in `tx-pool/src/util.rs` at lines 42–52 explicitly acknowledges the discrepancy with a comment:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

The admission flow in `_process_tx` (`tx-pool/src/process.rs`, lines 705–776) is:
1. `pre_check` → calls `check_tx_fee` with `tx_size` only (line 289/294)
2. `verify_rtx` → executes scripts, actual cycles become known (line 724–732)
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` is created (line 751) — **no second fee check against weight is performed**

For local RPC submissions (`send_transaction`), `declared_cycles` is `None`, so `max_cycles = self.consensus.max_block_cycles()` (line 720), meaning a tx can consume up to the full block cycle budget.

Meanwhile, `TxEntry::fee_rate()` in `tx-pool/src/component/entry.rs` (lines 114–118) correctly uses weight:
```rust
pub fn fee_rate(&self) -> FeeRate {
    let weight = get_transaction_weight(self.size, self.cycles);
    FeeRate::calculate(self.fee, weight)
}
```

The pool eviction (`limit_size`) and block assembler also correctly use weight-based fee rate. The structural gap is exclusively at the admission gate: `check_tx_fee` uses `tx_size`, but the resource actually consumed is `weight = max(tx_size, cycles × 0.000_170_571_4)`.

## Impact Explanation
This matches the allowed CKB bounty impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**.

An attacker submits a ~300-byte transaction whose lock script loops to consume a large number of cycles (up to `max_block_cycles`). The fee check passes because `min_fee = min_fee_rate × 300 / 1000`. The transaction is admitted and, if packaged, occupies a disproportionate share of the block cycle budget while paying fees as if it were a 300-byte transaction. At `min_fee_rate = 1000 shannons/KB`, the cost is ~0.3 shannons per transaction — negligible. Repeating this across blocks allows the attacker to continuously crowd out legitimate cycle-heavy transactions at a tiny fraction of the fair cost.

## Likelihood Explanation
- **Entry path**: Any unprivileged caller of the `send_transaction` RPC can trigger this. No special role or privilege is required.
- **Script availability**: A cycle-consuming script (tight loop in CKB-VM) can be deployed by anyone as a cell and referenced as a cell dep.
- **Cost**: The attacker pays `min_fee_rate × tx_size` shannons per transaction — effectively negligible.
- **Repeatability**: The attack can be sustained across many blocks. While the pool eviction mechanism (using weight-based fee rate) will eventually evict low-fee-rate cycle-heavy txs when the pool is full, the attacker can continuously resubmit, keeping the cycle budget occupied at low cost.

## Recommendation
After `verify_rtx` returns the actual cycle count, perform a second fee check using the true weight:

```rust
// After cycles are known:
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Err(Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee_by_weight.as_u64(), fee.as_u64()));
}
```

Alternatively, require submitters to declare cycles upfront (as is already done for relayed transactions via the `declared_cycles` parameter) and use the declared value in the initial fee check, then verify the declaration matches actual execution.

## Proof of Concept
1. Deploy a CKB-VM script cell that executes a tight loop consuming `N` cycles (e.g., `N ≈ max_block_cycles`).
2. Construct a transaction:
   - One input cell (any live cell the attacker owns)
   - One output cell (change back to attacker)
   - Cell dep referencing the loop script
   - Lock script = the loop script
   - Serialized size ≈ 300 bytes
   - Fee = `min_fee_rate × 300 / 1000` shannons (e.g., 0.3 shannons at 1000 shannons/KW)
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` passes: `fee ≥ min_fee_rate × 300 / 1000` (uses `tx_size = 300`, not weight).
5. `verify_rtx` executes the script; actual cycles ≈ `N`; actual weight ≈ `N × 0.000_170_571_4`. No second fee check occurs.
6. The transaction enters the pool with a weight-based fee rate far below `min_fee_rate`.
7. The block assembler may package it (consuming the cycle budget) or the pool eviction will eventually remove it — but the attacker can immediately resubmit, sustaining the attack at negligible cost.