Audit Report

## Title
Tx-Pool Admission Uses Size-Only Fee Check While Post-Verification Weight Uses Cycles — (`tx-pool/src/util.rs`)

## Summary
`check_tx_fee` enforces the minimum fee rate using only the serialized transaction byte size, while all downstream pool operations (eviction, scoring, fee estimation) use `get_transaction_weight`, which takes cycles into account. A transaction with a small serialized size but high script-execution cycles can pass the size-based admission gate, force the node to execute up to `max_tx_verify_cycles` of script verification, and then enter the pool with a weight-based fee rate far below `min_fee_rate`. There is no second fee-rate check after `verify_rtx` returns the actual cycle count.

## Finding Description
In `tx-pool/src/util.rs` lines 42–52, `check_tx_fee` explicitly uses `tx_size` for the minimum fee calculation, with a comment acknowledging this is intentional as a "cheap check":

```rust
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee { return Err(Reject::LowFeeRate(...)); }
```

In `tx-pool/src/process.rs`, the `_process_tx` function (lines 705–777) calls `pre_check` (which invokes `check_tx_fee`) at line 715, then calls `verify_rtx` at line 724 to obtain the actual cycle count. After `verify_rtx` returns, the entry is unconditionally constructed at line 751:

```rust
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
```

No weight-based fee check is performed after `verified.cycles` is known. The entry is then submitted directly.

Meanwhile, `TxEntry::fee_rate()` (`entry.rs` lines 114–118), `EvictKey` (`entry.rs` lines 234–247), and `AncestorsScoreSortKey` (`entry.rs` lines 221–231) all compute fee rate using `get_transaction_weight(size, cycles)`, which is `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`.

With `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` and `max_tx_verify_cycles = 70,000,000`:
- A 200-byte tx with 70M cycles has weight ≈ 11,940
- Size-based `min_fee = 1000 × 200 / 1000 = 200 shannons` → **passes admission**
- Weight-based fee rate = `200 / 11,940 × 1000 ≈ 16.7 shannons/KW` → **~60× below `min_fee_rate`**

## Impact Explanation
An unprivileged attacker can repeatedly submit transactions that pass the size-based admission check, force the node to execute up to 70M cycles of script verification per transaction, and then immediately become the top eviction candidate. The CPU cost of verification is paid by the node before eviction occurs. This cycle repeats indefinitely at minimal cost to the attacker (200 shannons per transaction), constituting a CPU-exhaustion DoS. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
No special privilege is required. Any `send_transaction` RPC caller or P2P peer relaying transactions can trigger this. Constructing a CKB transaction with a tight loop in CKB-VM consuming near-maximum cycles is straightforward. The attack is repeatable indefinitely, and there is no rate limiting on transaction submission visible in the code path.

## Recommendation
After `verify_rtx` returns the actual cycle count, perform a second fee-rate check using the true weight before constructing the `TxEntry`:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(
        tx_pool.config.min_fee_rate, min_fee_by_weight.as_u64(), fee.as_u64()
    )), snapshot));
}
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
```

This ensures the admission gate uses the same cost metric as eviction and scoring, eliminating the inconsistency.

## Proof of Concept
1. Construct a CKB transaction with a single input/output (serialized size ≈ 200 bytes) and a lock script that executes a tight loop consuming ≈ 70,000,000 cycles.
2. Set fee = `ceil(min_fee_rate × tx_size / 1000)` = 200 shannons.
3. Submit via `send_transaction` RPC to a node with default config (`min_fee_rate = 1000`).
4. Observe: the node accepts the transaction (no `LowFeeRate` rejection at `check_tx_fee`), runs full script verification consuming ~70M cycles, admits the entry with effective weight-based fee rate ≈ 16.7 shannons/KW, then immediately marks it as the lowest-priority eviction candidate.
5. Repeat in a loop. Each iteration costs the attacker 200 shannons and forces the node to spend ~70M cycles of verification work.