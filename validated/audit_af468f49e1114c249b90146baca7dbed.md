Audit Report

## Title
Tx-Pool Admission Fee-Rate Check Uses Size-Only Weight While Stored Fee Rate Uses Full Weight Formula, Allowing Below-`min_fee_rate` Transactions Into the Pool - (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` gates pool admission using only serialized transaction size as the weight denominator, while `TxEntry::fee_rate()` and all downstream accounting use `get_transaction_weight(size, cycles)` = `max(size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For compute-heavy transactions where `cycles * DEFAULT_BYTES_PER_CYCLES > tx_size`, the admission threshold is materially looser than the weight-based minimum, allowing transactions whose true fee rate is far below `min_fee_rate` to enter the pool and force expensive script verification at a fraction of the intended cost.

## Finding Description

**Admission check** (`tx-pool/src/util.rs`, lines 42–52): The comment explicitly acknowledges the imprecision ("here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly"), but does not account for the security consequence. The minimum fee is computed as `min_fee_rate * tx_size / 1000`, using raw serialized size as the sole weight.

**Processing order** (`tx-pool/src/process.rs`, lines 715–751): `pre_check` (which calls `check_tx_fee`) runs *before* `verify_rtx`. Cycles are unknown at admission time. After `verify_rtx` returns `verified.cycles`, there is **no secondary fee-rate check** using the full weight formula — the entry is immediately constructed and submitted: `TxEntry::new(rtx, verified.cycles, fee, tx_size)`.

**Stored fee rate** (`tx-pool/src/component/entry.rs`, lines 114–118): `fee_rate()` uses `get_transaction_weight(self.size, self.cycles)` = `max(size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For any transaction where `cycles * 0.000_170_571_4 > tx_size`, the stored fee rate is strictly below `min_fee_rate`.

**Exploit path:**
1. Attacker crafts a transaction with small serialized size (`tx_size`) and high script cycles.
2. Sets fee = `min_fee_rate * tx_size / 1000 + 1` — just above the size-only threshold.
3. `check_tx_fee` passes (size-only check).
4. Node runs full script verification consuming up to `max_block_cycles` cycles.
5. Transaction enters the pool with a weight-based fee rate far below `min_fee_rate`.
6. No guard exists to reject it post-verification.

**RPC misleading** (`tx-pool/src/service.rs`, line 1091): `tx_pool_info` reports `min_fee_rate` as the enforced threshold, but the pool already contains entries admitted below it.

## Impact Explanation

This is a **High** severity finding matching the allowed impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

The discount is extreme. For a transaction with `tx_size = 100` bytes and `cycles = max_block_cycles ≈ 3,500,000,000`:
- `cycles * DEFAULT_BYTES_PER_CYCLES ≈ 597,000` weight-bytes
- Admission fee paid: `min_fee_rate * 100 / 1000`
- Fee that should be required: `min_fee_rate * 597,000 / 1000`
- **Effective discount: ~6000×**

An attacker can repeatedly submit maximum-cycle, minimum-size transactions at ~0.017% of the intended cost, forcing each node to run full script verification (bounded by `max_block_cycles`) per submission. This saturates the verify queue and CPU with expensive work paid for at a negligible fee, causing sustained node-level and network-level congestion.

## Likelihood Explanation

Any unprivileged RPC caller via `send_transaction` can trigger this. CKB lock/type scripts routinely consume millions of cycles with compact transaction bodies. The condition `cycles * 0.000_170_571_4 > tx_size` is easily satisfied by any moderately complex script. No special privileges, keys, or hashpower are required. The attack is repeatable and cheap.

## Recommendation

After `verify_rtx` returns `verified.cycles` in `_process_tx`, add a secondary fee-rate check using the full weight formula before constructing the `TxEntry`:

```rust
let verified = try_or_return_with_snapshot!(verified_ret, snapshot);
// Secondary check: enforce min_fee_rate against actual weight
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

The pre-check size-only gate can remain as a cheap early filter, but the authoritative rejection must happen post-verification using the same weight formula as `TxEntry::fee_rate()`.

## Proof of Concept

**Parameters:** `min_fee_rate = 1000 shannons/KW`, `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`.

**Transaction:** `tx_size = 200` bytes, `cycles = 5_000_000`, `fee = 201` shannons.

**Step 1 — Admission (`check_tx_fee`):**
- `min_fee = 1000 * 200 / 1000 = 200`
- `fee (201) >= min_fee (200)` → **admitted**, node proceeds to full script verification.

**Step 2 — Verification (`verify_rtx`):**
- Node executes scripts consuming 5,000,000 cycles.
- Returns `verified.cycles = 5_000_000`.

**Step 3 — Pool entry (`TxEntry::fee_rate`):**
- `weight = max(200, floor(5_000_000 * 0.000_170_571_4)) = max(200, 852) = 852`
- `fee_rate = 201 * 1000 / 852 ≈ 235 shannons/KW`
- `235 < 1000 (min_fee_rate)` → **below threshold, in pool**.

**Step 4 — RPC (`tx_pool_info`):**
- Reports `min_fee_rate = 1000`, while pool contains entry at ~235.

Scaling to `tx_size = 100`, `cycles = 3_500_000_000` (max block cycles): attacker pays 100 shannons, forces ~597,000-weight-unit verification work — a ~6000× cost asymmetry exploitable in a loop to congest any CKB node's verify pipeline.